# Copyright Hybrid Logic Ltd.  See LICENSE file for details.
"""
Tests for ``flocker.control.httpapi``.
"""

from io import BytesIO
from uuid import uuid4

from zope.interface.verify import verifyObject

from twisted.internet import reactor
from twisted.trial.unittest import SynchronousTestCase
from twisted.test.proto_helpers import MemoryReactor
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.web.http import OK, CONFLICT, BAD_REQUEST
from twisted.web.http_headers import Headers
from twisted.web.server import Site
from twisted.web.client import FileBodyProducer, readBody
from twisted.application.service import IService
from twisted.python.filepath import FilePath

from ...restapi.testtools import (
    buildIntegrationTests, dumps, loads, goodResult, badResult)

from .. import Dataset, Manifestation, Node, Deployment
from ..httpapi import DatasetAPIUserV1, create_api_service
from .._persistence import ConfigurationPersistenceService
from ... import __version__


class APITestsMixin(object):
    """
    Helpers for writing integration tests for the Dataset Manager API.
    """
    def initialize(self):
        """
        Create initial objects for the ``DatasetAPIUserV1``.
        """
        self.persistence_service = ConfigurationPersistenceService(
            reactor, FilePath(self.mktemp()))
        self.persistence_service.startService()
        self.addCleanup(self.persistence_service.stopService)

    def assertResponseCode(self, method, path, request_body, expected_code):
        if request_body is None:
            headers = None
            body_producer = None
        else:
            headers = Headers({b"content-type": [b"application/json"]})
            body_producer = FileBodyProducer(BytesIO(dumps(request_body)))

        requesting = self.agent.request(
            method, path, headers, body_producer)

        def check_code(response):
            self.assertEqual(expected_code, response.code)
            return response
        requesting.addCallback(check_code)
        return requesting

    def assertGoodResult(self, method, path, expected_good_result):
        """
        Assert a particular JSON response for the given API request.

        :param bytes method: HTTP method to request.
        :param bytes path: HTTP path.
        :param unicode expected_good_result: Successful good result we expect.

        :return Deferred: Fires when test is done.
        """
        requesting = self.assertResponseCode(method, path, None, OK)
        requesting.addCallback(readBody)
        requesting.addCallback(lambda body: self.assertEqual(
            goodResult(expected_good_result), loads(body)))
        return requesting

    def assertBadResult(self, method, path, request_body,
                        expected_code, expected_bad_result):
        """
        Assert a particular JSON response for the given API request.

        :param bytes method: HTTP method to request.
        :param bytes path: HTTP path.
        :param unicode expected_bad_result: The result expected in the error
            respones.

        :return Deferred: Fires when test is done.
        """
        requesting = self.assertResponseCode(
            method, path, request_body, expected_code)
        requesting.addCallback(readBody)
        requesting.addCallback(lambda body: self.assertEqual(
            badResult(expected_bad_result), loads(body)))
        return requesting


class VersionTestsMixin(APITestsMixin):
    """
    Tests for the service version description endpoint at ``/version``.
    """
    def test_version(self):
        """
        The ``/version`` command returns JSON-encoded ``__version__``.
        """
        return self.assertGoodResult(b"GET", b"/version",
                                     {u'flocker': __version__})


def _build_app(test):
    test.initialize()
    return DatasetAPIUserV1(test.persistence_service).app

RealTestsVersion, MemoryTestsVersion = buildIntegrationTests(
    VersionTestsMixin, "Version", _build_app)


class CreateDatasetTestsMixin(APITestsMixin):
    """
    Tests for the dataset creation endpoint at ``/datasets``.
    """
    NODE_A = b"10.0.0.1"

    def test_wrong_schema(self):
        """
        If a ``POST`` request made to the endpoint includes a body which
        doesn't match the ``definitions/datasets`` schema, the response is an
        error indication a validation failure.
        """
        return self.assertBadResult(
            b"POST", b"/datasets", {u"primary": self.NODE_A, u"junk": u"garbage"},
            BAD_REQUEST,
            {u'description': u"The provided JSON doesn't match the required schema.",
             u'errors': [
                 u"Additional properties are not allowed (u'junk' was unexpected)"]}
        )

    def test_dataset_id_collision(self):
        """
        If the value for the ``dataset_id`` in the request body is already
        assigned to an existing dataset, the response is an error indicating
        the collision and the dataset is not added to the desired
        configuration.
        """
        dataset_id = unicode(uuid4())
        existing_dataset = Dataset(dataset_id=dataset_id)
        existing_manifestation = Manifestation(
            dataset=existing_dataset, primary=True)

        saving = self.persistence_service.save(Deployment(
            nodes={
                Node(
                    hostname=self.NODE_A,
                    other_manifestations=frozenset({existing_manifestation})
                )
            }
        ))

        def saved(ignored):
            return self.assertBadResult(
                b"POST", b"/datasets",
                {u"primary": self.NODE_A, u"dataset_id": dataset_id},
                CONFLICT,
                {u"description": u"The provided dataset_id is already in use."}
            )
        posting = saving.addCallback(saved)

        def failed(reason):
            deployment = self.persistence_service.get()
            (node_a,) = deployment.nodes
            self.assertEqual(
                frozenset({existing_manifestation}),
                node_a.other_manifestations
            )

        posting.addCallback(failed)
        return posting

    def test_unknown_primary_node(self):
        """
        If a ``POST`` request made to the endpoint indicates a non-existent
        node as the location of the primary manifestation, the configuration is
        unchanged and an error response is returned to the client.
        """
        return self.assertBadResult(
            b"POST", b"/datasets", {u"primary": self.NODE_A},
            BAD_REQUEST, {
                u"description":
                    u"The provided primary node is not part of the cluster."
            }
        )

    def test_minimal_create_dataset(self):
        """
        If a ``POST`` request made to the endpoint includes just the minimum
        information necessary to create a new dataset (an identifier of the
        node on which to place its primary manifestation) then the desired
        configuration is updated to include a new unattached manifestation of
        the new dataset with a newly generated dataset identifier and a
        description of the new dataset is returned in a success response to the
        client.
        """
        saving = self.persistence_service.save(Deployment(
            nodes=frozenset({Node(hostname=self.NODE_A)})
        ))
        def saved(ignored):
            creating = self.assertResponseCode(
                b"POST", b"/datasets", {u"primary": self.NODE_A},
                OK)
            creating.addCallback(readBody)
            creating.addCallback(loads)
            return creating
        creating = saving.addCallback(saved)

        def got_result(result):
            result = result[u"result"]
            dataset_id = result.pop(u"dataset_id")
            self.assertEqual({u"primary": self.NODE_A, u"metadata": {}}, result)
            deployment = self.persistence_service.get()
            self.assertEqual({dataset_id}, set(get_dataset_ids(deployment)))
        creating.addCallback(got_result)

        return creating

    def test_create_with_metadata(self):
        # verify given metadata is persisted, success response includes it
        pass

    # ... etc


def get_dataset_ids(deployment):
    for node in deployment.nodes:
        for manifestation in node.manifestations():
            yield manifestation.dataset.dataset_id

RealTestsCreateDataset, MemoryTestsCreateDataset = buildIntegrationTests(
    CreateDatasetTestsMixin, "CreateDataset", _build_app)


class CreateAPIServiceTests(SynchronousTestCase):
    """
    Tests for ``create_api_service``.
    """
    def test_returns_service(self):
        """
        ``create_api_service`` returns an object providing ``IService``.
        """
        reactor = MemoryReactor()
        endpoint = TCP4ServerEndpoint(reactor, 6789)
        verifyObject(IService, create_api_service(None, endpoint))

    def test_listens_endpoint(self):
        """
        ``create_api_service`` returns a service that listens using the given
        endpoint with a HTTP server.
        """
        reactor = MemoryReactor()
        endpoint = TCP4ServerEndpoint(reactor, 6789)
        service = create_api_service(None, endpoint)
        self.addCleanup(service.stopService)
        service.startService()
        server = reactor.tcpServers[0]
        port = server[0]
        factory = server[1].__class__
        self.assertEqual((port, factory), (6789, Site))
