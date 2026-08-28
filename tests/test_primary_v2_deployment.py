from __future__ import annotations

import unittest
from copy import deepcopy

from scripts.primary_v1_deployment import (
    DEFAULT_MANIFEST as V1_MANIFEST,
    validate_documents as validate_v1,
)
from scripts.primary_v2_deployment import (
    DEFAULT_MANIFEST,
    DeploymentContractError,
    TEMPLATE_IMAGE,
    load_documents,
    render_documents,
    validate_documents,
)


IMAGE = "registry.example.test/xuanji-mini@sha256:" + "b" * 64


class PrimaryV2DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.documents = load_documents(DEFAULT_MANIFEST)

    def test_committed_template_has_isolated_v2_resources(self):
        validate_documents(self.documents, allow_template_image=True)
        names = {
            (item["kind"], item["metadata"]["name"])
            for item in self.documents
        }
        self.assertIn(("Deployment", "xuanji-primary-v2"), names)
        self.assertIn(("PersistentVolumeClaim", "xuanji-primary-v2-data"), names)
        self.assertEqual(TEMPLATE_IMAGE, self._container(self.documents)["image"])
        config = self._document(self.documents, "ConfigMap")["data"]
        self.assertEqual("primary_v2", config["XUANJI_ANALYSIS_PROFILE"])
        self.assertEqual("xuanji-primary-host-v1", config["XUANJI_RECEIPT_KEY_ID"])

    def test_render_preserves_v2_profile_and_immutable_image(self):
        rendered = render_documents(
            self.documents,
            image=IMAGE,
            host_public_url="http://xuanji-primary-v2:8091",
            dview_mcp_url="https://dview.example.test/mcp/query",
        )
        validate_documents(rendered, allow_template_image=False)
        self.assertEqual(IMAGE, self._container(rendered)["image"])
        config = self._document(rendered, "ConfigMap")["data"]
        self.assertEqual("primary_v2", config["XUANJI_ANALYSIS_PROFILE"])

    def test_v1_and_v2_verifiers_reject_the_opposite_profile(self):
        with self.assertRaises(DeploymentContractError):
            validate_v1(self.documents, allow_template_image=True)
        with self.assertRaises(DeploymentContractError):
            validate_documents(
                load_documents(V1_MANIFEST), allow_template_image=True
            )

    def test_profile_identity_and_shadow_ingress_drift_fail_closed(self):
        profile = deepcopy(self.documents)
        self._document(profile, "ConfigMap")["data"][
            "XUANJI_ANALYSIS_PROFILE"
        ] = "primary_v1"
        with self.assertRaises(DeploymentContractError):
            validate_documents(profile, allow_template_image=True)

        ingress = deepcopy(self.documents)
        self._document(ingress, "NetworkPolicy")["spec"]["ingress"][0][
            "from"
        ][0]["podSelector"]["matchLabels"] = {
            "xuanji.taptap/client": "true"
        }
        with self.assertRaises(DeploymentContractError):
            validate_documents(ingress, allow_template_image=True)

    def test_v2_secret_and_pvc_identity_cannot_reuse_v1(self):
        secrets = deepcopy(self.documents)
        self._container(secrets)["env"][0]["valueFrom"]["secretKeyRef"][
            "name"
        ] = "xuanji-primary-v1-host-auth"
        with self.assertRaises(DeploymentContractError):
            validate_documents(secrets, allow_template_image=True)

        volume = deepcopy(self.documents)
        self._document(volume, "Deployment")["spec"]["template"]["spec"][
            "volumes"
        ][0]["persistentVolumeClaim"]["claimName"] = "xuanji-primary-v1-data"
        with self.assertRaises(DeploymentContractError):
            validate_documents(volume, allow_template_image=True)

    @staticmethod
    def _document(documents, kind):
        return next(item for item in documents if item["kind"] == kind)

    @classmethod
    def _container(cls, documents):
        return cls._document(documents, "Deployment")["spec"]["template"][
            "spec"
        ]["containers"][0]


if __name__ == "__main__":
    unittest.main()
