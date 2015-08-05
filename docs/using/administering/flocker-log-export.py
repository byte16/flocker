# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
A script to export Flocker log files and system information.
"""

from gzip import open as gzip_open
import os
from platform import dist as platform_dist
from shutil import copyfileobj, make_archive, rmtree
from socket import gethostname
import sys
from subprocess import check_call, check_output
from tarfile import open as tarfile_open
from time import time


class FlockerDebugArchive(object):
    """
    Create a tar archive containing:
    * logs from all installed Flocker services,
    * some or all of the syslog depending on the logging system,
    * Docker version and configuration information, and
    * a list of all the services installed on the system and their status.
    """
    def __init__(self, service_manager, log_exporter):
        """
        :param service_manager: An API for listing installed services.
        :param log_exporter: An API for exporting logs for services.
        """
        self._service_manager = service_manager
        self._log_exporter = log_exporter

        self._suffix = "{}_{}".format(
            gethostname(),
            time()
        )
        self._archive_name = "clusterhq_flocker_logs_{}".format(
            self._suffix
        )
        self._archive_path = os.path.abspath(self._archive_name)

    def _logfile_path(self, name):
        """
        Generate a path to a file inside the archive directory. The file name
        will have a suffix that matches the name of the archive.

        :param str name: A unique label for the file.
        :returns: An absolute path string for a file inside the archive
            directory.
        """
        return os.path.join(
            self._archive_name,
            '{}-{}'.format(name, self._suffix)
        )

    def _open_logfile(self, name):
        """
        :param str name: A unique label for the file.
        :return: An open ``file`` object with a name generated by
            `_logfile_path`.
        """
        return open(self._logfile_path(name), 'w')

    def create(self):
        """
        Create the archive by first creating a uniquely named directory in the
        current working directory, adding the log files and debug information,
        creating a ``tar`` archive from the directory and finally removing the
        directory.
        """
        os.makedirs(self._archive_path)
        try:
            # Export Flocker logs.
            services = self._service_manager.flocker_services()
            for service_name, service_status in services:
                self._log_exporter.export_flocker(
                    service_name=service_name,
                    target_path=self._logfile_path(service_name)
                )
            # Export syslog.
            self._log_exporter.export_all(self._logfile_path('syslog'))

            # Export the status of all services.
            with self._open_logfile('service-status') as output:
                services = self._service_manager.all_services()
                for service_name, service_status in services:
                    output.write(service_name + " " + service_status + "\n")

            # Export Docker version and configuration
            check_call(
                ['docker', 'version'],
                stdout=self._open_logfile('docker_version')
            )
            check_call(
                ['docker', 'info'],
                stdout=self._open_logfile('docker_info')
            )

            # Export Kernel version
            self._open_logfile('uname').write(' '.join(os.uname()))

            # Export Distribution version
            self._open_logfile('os-release').write(
                open('/etc/os-release').read()
            )

            # Create a single archive file
            archive_path = make_archive(
                base_name=self._archive_name,
                format='tar',
                root_dir=os.path.dirname(self._archive_path),
                base_dir=os.path.basename(self._archive_path),
            )
        finally:
            # Attempt to remove the source directory.
            rmtree(self._archive_path)
        return archive_path


class SystemdServiceManager(object):
    """
    List services managed by Systemd.
    """
    def all_services(self):
        """
        Iterate the name and status of all services known to SystemD.
        """
        output = check_output(['systemctl', 'list-unit-files', '--no-legend'])
        for line in output.splitlines():
            service_name, service_status = line.split(None, 1)
            yield service_name, service_status

    def flocker_services(self):
        """
        Iterate the name and status of the Flocker services known to SystemD.
        """
        for service_name, service_status in self.all_services():
            if service_name.startswith('flocker-'):
                yield service_name, service_status


class UpstartServiceManager(object):
    """
    List services managed by Upstart.
    """
    def all_services(self):
        """
        Iterate the name and status of all services known to Upstart.
        """
        for line in check_output(['initctl', 'list']).splitlines():
            service_name, service_status = line.split(None, 1)
            yield service_name, service_status

    def flocker_services(self):
        """
        Iterate the name and status of the Flocker services known to Upstart.
        """
        for service_name, service_status in self.all_services():
            if service_name.startswith('flocker-'):
                yield service_name, service_status


class JournaldLogExporter(object):
    """
    Export logs managed by JournalD.
    """
    def export_flocker(self, service_name, target_path):
        """
        Export logs for ``service_name`` to ``target_path`` compressed using
        ``gzip``.
        """
        check_call(
            'journalctl --all --output cat --unit {unit} '
            '| gzip'.format(service_name),
            stdout=open(target_path + '.gz', 'w'),
            shell=True
        )

    def export_all(self, target_path):
        """
        Export all system logs to ``target_path`` compressed using ``gzip``.
        """
        check_call(
            'journalctl --all --boot | gzip',
            stdout=open(target_path + '.gz', 'w'),
            shell=True
        )


class UpstartLogExporter(object):
    """
    Export logs for services managed by Upstart and written by RSyslog.
    """
    def export_flocker(self, service_name, target_path):
        """
        Export both the Upstart startup logs and the Eliot logs for
        ``service_name`` to a gzip compressed tar file at ``target_path``.
        """
        with tarfile_open(target_path + '.tar.gz', 'w|gz') as tar:
            files = [
                ("/var/log/upstart/{}.log".format(service_name),
                 service_name + '-upstart.log'),
                ("/var/log/flocker/{}.log".format(service_name),
                 service_name + '-eliot.log'),
            ]
            for input_path, archive_name in files:
                if os.path.isfile(input_path):
                    tar.add(input_path, arcname=archive_name)

    def export_all(self, target_path):
        """
        Export all system logs to ``target_path`` compressed using ``gzip``.
        """
        with open('/var/log/syslog', 'rb') as f_in:
            with gzip_open(target_path + '.gz', 'wb') as f_out:
                copyfileobj(f_in, f_out)


class Platform(object):
    """
    A record of the service manager and log exported to be used on each
    supported operating system.
    """
    def __init__(self, name, version, service_manager, log_exporter):
        """
        :param str name: The name of the operating system.
        :param str version: The version of the operating system.
        :param service_manager: The service manager API to use for this
            operating system.
        :param log_exporter: The log exporter API to use for this operating
            system.
        """
        self.name = name
        self.version = version
        self.service_manager = service_manager
        self.log_exporter = log_exporter


PLATFORMS = (
    Platform(
        name='centos',
        version='7',
        service_manager=SystemdServiceManager(),
        log_exporter=JournaldLogExporter()
    ),
    Platform(
        name='ubuntu',
        version='14.04',
        service_manager=UpstartServiceManager(),
        log_exporter=UpstartLogExporter()
    )
)


_PLATFORM_BY_LABEL = dict(
    ('{}-{}'.format(p.name, p.version), p)
    for p in PLATFORMS
)


class UnsupportedDistribution(Exception):
    """
    The distribution is not supported.
    """
    def __init__(self, distribution):
        """
        :param str distribution: The unsupported distribution.
        """
        self.distribution = distribution


def current_platform():
    """
    :returns: A ``Platform`` for the operating system where this script.
    :raises: ``UnsupportedPlatform`` if the current platform is unsupported.
    """
    name, version, nickname = platform_dist()
    distribution = name.lower() + '-' + version
    for supported_distribution, platform in _PLATFORM_BY_LABEL.items():
        if distribution.startswith(supported_distribution):
            return platform
    else:
        raise UnsupportedDistribution(distribution)


def main():
    try:
        platform = current_platform()
    except UnsupportedDistribution as e:
        sys.stderr.write(
            "ERROR: flocker-log-export "
            "is not supported on this operating system ({!r}).\n"
            "See https://docs.clusterhq.com/en/latest/using/administering/debugging.html \n"  # noqa
            "for alternative ways to export Flocker logs "
            "and diagnostic data.\n".format(e.distribution)
        )
        sys.exit(1)

    archive_path = FlockerDebugArchive(
        service_manager=platform.service_manager,
        log_exporter=platform.log_exporter
    ).create()
    sys.stdout.write(archive_path + '\n')


if __name__ == "__main__":
    raise SystemExit(main())
