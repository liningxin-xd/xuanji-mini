from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

from scripts.primary_v1_deployment import (
    DEFAULT_MANIFEST,
    DeploymentContractError,
    TEMPLATE_IMAGE,
    dump_documents,
    load_documents,
    render_documents,
    validate_documents,
)


IMAGE = "registry.example.test/xuanji-mini@sha256:" + "a" * 64


class PrimaryV1DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.documents = load_documents(DEFAULT_MANIFEST)

    def test_committed_template_satisfies_the_deployment_contract(self):
        validate_documents(self.documents, allow_template_image=True)
        kinds = {item["kind"] for item in self.documents}
        self.assertNotIn("Secret", kinds)
        self.assertEqual(TEMPLATE_IMAGE, self._container(self.documents)["image"])

    def test_render_requires_and_preserves_an_immutable_image(self):
        rendered = render_documents(
            self.documents,
            image=IMAGE,
            host_public_url="http://xuanji.internal:8091",
            dview_mcp_url="https://dview.example.test/mcp/query",
        )
        validate_documents(rendered, allow_template_image=False)
        self.assertEqual(IMAGE, self._container(rendered)["image"])
        config = self._document(rendered, "ConfigMap")["data"]
        self.assertEqual(
            "http://xuanji.internal:8091", config["XUANJI_HOST_PUBLIC_URL"]
        )
        self.assertEqual(
            "https://dview.example.test/mcp/query",
            config["XUANJI_DVIEW_MCP_URL"],
        )
        self.assertEqual("primary_v1", config["XUANJI_ANALYSIS_PROFILE"])

    def test_tagged_or_placeholder_images_fail_the_release_gate(self):
        for image in ("registry.example.test/xuanji-mini:latest", TEMPLATE_IMAGE):
            with self.subTest(image=image):
                with self.assertRaises(DeploymentContractError):
                    render_documents(
                        self.documents,
                        image=image,
                        host_public_url="http://xuanji.internal:8091",
                        dview_mcp_url="https://dview.example.test/mcp/query",
                    )

    def test_replica_or_secret_contract_drift_is_rejected(self):
        replicas = deepcopy(self.documents)
        self._document(replicas, "Deployment")["spec"]["replicas"] = 2
        with self.assertRaises(DeploymentContractError):
            validate_documents(replicas, allow_template_image=True)

        secrets = deepcopy(self.documents)
        env = self._container(secrets)["env"]
        env[1]["valueFrom"]["secretKeyRef"]["name"] = (
            "xuanji-primary-v1-host-auth"
        )
        with self.assertRaises(DeploymentContractError):
            validate_documents(secrets, allow_template_image=True)

        network = deepcopy(self.documents)
        self._document(network, "NetworkPolicy")["spec"]["ingress"] = [{}]
        with self.assertRaises(DeploymentContractError):
            validate_documents(network, allow_template_image=True)

        profile = deepcopy(self.documents)
        self._document(profile, "ConfigMap")["data"][
            "XUANJI_ANALYSIS_PROFILE"
        ] = "primary_v2"
        with self.assertRaises(DeploymentContractError):
            validate_documents(profile, allow_template_image=True)

    def test_rendered_yaml_round_trips_without_secret_documents(self):
        rendered = render_documents(
            self.documents,
            image=IMAGE,
            host_public_url="http://xuanji.internal:8091",
            dview_mcp_url="https://dview.example.test/mcp/query",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "deployment.yaml"
            path.write_text(dump_documents(rendered), encoding="utf-8")
            loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        validate_documents(loaded, allow_template_image=False)
        self.assertNotIn("Secret", {item["kind"] for item in loaded})

    @staticmethod
    def _document(documents, kind):
        return next(item for item in documents if item["kind"] == kind)

    @classmethod
    def _container(cls, documents):
        return cls._document(documents, "Deployment")["spec"]["template"]["spec"][
            "containers"
        ][0]


if __name__ == "__main__":
    unittest.main()
