"""Tests for KubernetesCollector token resolution logic.

Covers:
- Explicit token via auth_env environment variable.
- Fallback to kubeconfig token extraction.
- Missing token yields None.
"""

import os
import pathlib

import pytest

from collectors.integration_base import IntegrationConfig
from collectors.kubernetes_collector import KubernetesCollector, _load_kubeconfig_token

@pytest.fixture
def base_cfg():
    # Minimal valid config for KubernetesCollector; endpoint is required.
    return IntegrationConfig(
        source="kubernetes",
        endpoint="https://example.com",
        auth_scheme="bearer",
    )

def test_resolve_secret_prefers_env_var(monkeypatch, base_cfg):
    # Set explicit env var and config auth_env pointing to it.
    monkeypatch.setenv("AUTORCA_K8S_TOKEN", "env-token-123")
    cfg = IntegrationConfig(
        source="kubernetes",
        endpoint="https://example.com",
        auth_env="AUTORCA_K8S_TOKEN",
        auth_scheme="bearer",
    )
    collector = KubernetesCollector(cfg)
    secret = collector._resolve_secret()
    assert secret == "env-token-123"

def test_resolve_secret_falls_back_to_kubeconfig(monkeypatch, tmp_path, base_cfg):
    # Create a minimal kubeconfig with a token for the current context.
    kubeconfig_content = """
apiVersion: v1
clusters:
- cluster:
    server: https://1.2.3.4
  name: test-cluster
contexts:
- context:
    cluster: test-cluster
    user: test-user
  name: test-context
current-context: test-context
users:
- name: test-user
  user:
    token: kubeconfig-token-xyz
"""
    kubeconfig_path = tmp_path / "kubeconfig"
    kubeconfig_path.write_text(kubeconfig_content, encoding="utf-8")
    # Ensure the env var for token is not set.
    monkeypatch.delenv("AUTORCA_K8S_TOKEN", raising=False)
    # Point KUBECONFIG to our temporary file.
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig_path))
    cfg = IntegrationConfig(
        source="kubernetes",
        endpoint="https://example.com",
        auth_scheme="bearer",
    )
    collector = KubernetesCollector(cfg)
    secret = collector._resolve_secret()
    assert secret == "kubeconfig-token-xyz"

def test_load_kubeconfig_token_none_when_missing(monkeypatch, tmp_path):
    # Empty file – no token defined.
    empty_path = tmp_path / "empty"
    empty_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("KUBECONFIG", str(empty_path))
    assert _load_kubeconfig_token() is None
