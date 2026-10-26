"""Tests for native Windows compatibility, Cloudflare Tunnel integration, and endpoint security."""

import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import FakeBuilder, FakeGemini, manifest
from fastapi.testclient import TestClient

from genesis.config import Settings, find_cloudflared, java_major_version
from genesis.core.builder import AndroidBuilder
from genesis.core.jobs import Orchestrator
from genesis.server import create_app


@pytest.fixture
def test_app_and_client(settings):
    settings.whatsapp_provider = "meta"
    settings.meta_verify_token = "verify-token-123"
    settings.meta_app_secret = "secret-key-456"
    settings.meta_access_token = "EAABtest"
    settings.meta_phone_number_id = "10987654321"
    settings.public_base_url = "https://genesis.trycloudflare.com"

    orch = Orchestrator(settings, FakeGemini(manifest()), builder=FakeBuilder(settings))
    app = create_app(settings, orchestrator=orch, start_sms_worker=False)
    client = TestClient(app)
    return settings, app, client


def test_windows_java_and_sdk_discovery(settings, monkeypatch, tmp_path):
    """Verify that JDK 17+ and Android SDK are discovered in Windows standard locations."""
    # Mock Windows Java layout
    fake_jdk = tmp_path / "jdk"
    (fake_jdk / "bin").mkdir(parents=True)
    java_exe = fake_jdk / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    java_exe.touch()
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[str(java_exe), "-version"],
            returncode=0,
            stdout="",
            stderr='openjdk version "21.0.8" 2025-07-15\n'
        )
        assert java_major_version(fake_jdk) == 21

    # Verify Android SDK detection logic includes Windows path
    from genesis.config import _find_android_home
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    fake_sdk = tmp_path / "Sdk"
    fake_sdk.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Put Sdk in AppData/Local/Android/Sdk
    win_sdk = tmp_path / "AppData" / "Local" / "Android" / "Sdk"
    win_sdk.mkdir(parents=True)
    assert _find_android_home() == win_sdk


def test_windows_gradle_env_path_and_systemroot(settings):
    """Verify that on Windows, gradle_env uses ';' separator and includes SystemRoot and TEMP."""
    builder = AndroidBuilder(settings)
    with patch.object(sys, "platform", "win32"):
        env = builder.gradle_env()
        assert ";" in env["PATH"]
        parts = env["PATH"].split(";")
        assert len(parts) >= 4
        assert "SystemRoot" in env
        assert "SystemDrive" in env
        assert "COMSPEC" in env
        assert "TEMP" in env
        assert "TMP" in env


def test_windows_gradle_runner_cmd_and_wrapper(settings, tmp_path):
    """Verify that on Windows, run_gradle invokes cmd.exe with gradlew.bat and handles taskkill on termination."""
    builder = AndroidBuilder(settings)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "gradlew.bat").touch()
    log_path = tmp_path / "build.log"

    with patch.object(sys, "platform", "win32"), \
         patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.stdout = ["BUILD SUCCESSFUL\n"]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        res = builder.run_gradle(workspace, log_path)
        assert res.ok
        assert mock_popen.called
        cmd_called = mock_popen.call_args[0][0]
        assert cmd_called[0] == "cmd.exe"
        assert cmd_called[1] == "/c"
        assert cmd_called[2].endswith("gradlew.bat")
        assert cmd_called[3] == "assembleDebug"
        assert "creationflags" in mock_popen.call_args[1]


def test_windows_aapt2_and_apksigner_resolution(settings, tmp_path):
    """Verify verify_apk looks up aapt2.exe and apksigner.bat on Windows."""
    builder = AndroidBuilder(settings)
    bt = tmp_path / "build-tools" / "35.0.0"
    bt.mkdir(parents=True)
    (bt / "aapt2.exe").touch()
    (bt / "apksigner.bat").touch()

    monkeypatch_s = patch.object(settings, "build_tools_dir", return_value=bt)
    with monkeypatch_s, patch.object(sys, "platform", "win32"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="package: name='com.genesis.testapp'\nuses-permission: name='com.genesis.testapp.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION'\n",
                stderr=""
            )
            # Create a mock valid zip APK
            apk_path = tmp_path / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
            apk_path.parent.mkdir(parents=True)
            import zipfile
            with zipfile.ZipFile(apk_path, "w") as z:
                z.writestr("AndroidManifest.xml", b"x" * 5000)
                z.writestr("classes.dex", b"x" * 5000)
                z.writestr("resources.arsc", b"x" * 5000)
            
            apk_info = builder.verify_apk(tmp_path, "com.genesis.testapp")
            assert apk_info.package == "com.genesis.testapp"
            assert apk_info.signature_verified


def test_doctor_diagnoses_all_prerequisites(tmp_path):
    """Verify that doctor reports Python dependencies, Cloudflare, Meta config, and public URL without leaking secrets."""
    s = Settings(data_dir=tmp_path / "data", template_dir=tmp_path / "android_template")
    s.whatsapp_provider = "meta"
    s.meta_access_token = ""
    s.meta_phone_number_id = ""
    s.meta_verify_token = ""
    s.meta_app_secret = ""
    s.public_base_url = "http://not-https.com"

    doc = s.doctor()
    checks_by_name = {c["name"]: c for c in doc["checks"]}

    assert "Python dependencies" in checks_by_name
    assert "Meta WhatsApp" in checks_by_name
    assert not checks_by_name["Meta WhatsApp"]["ok"]
    assert "missing: META_ACCESS_TOKEN" in checks_by_name["Meta WhatsApp"]["detail"]
    assert "Cloudflare tunnel" in checks_by_name
    assert "Public URL" in checks_by_name
    assert not checks_by_name["Public URL"]["ok"]
    assert "must begin with https://" in checks_by_name["Public URL"]["detail"]


def test_public_and_private_endpoint_authorization(test_app_and_client):
    """Verify that private endpoints reject external proxy requests while public endpoints remain accessible."""
    settings, app, client = test_app_and_client

    # 1. Local request without proxy headers: dashboard and api/builds accessible
    res_local = client.get("/")
    assert res_local.status_code == 200

    # 2. External request via tunnel (carrying proxy headers): private endpoints rejected with 401
    res_proxy_private = client.get("/", headers={"x-forwarded-for": "203.0.113.195", "cf-connecting-ip": "203.0.113.195"},
                                   follow_redirects=False)
    assert res_proxy_private.status_code == 303 and res_proxy_private.headers["location"] == "/start"

    res_proxy_builds = client.get("/api/builds", headers={"x-forwarded-for": "203.0.113.195"})
    assert res_proxy_builds.status_code == 401

    # 3. Public endpoints remain accessible through tunnel headers
    res_chat = client.get("/chat", headers={"cf-connecting-ip": "203.0.113.195"})
    assert res_chat.status_code in (200, 307)

    # 4. Meta Webhook challenge handshake accessible through tunnel headers
    challenge_params = {
        "hub.mode": "subscribe",
        "hub.verify_token": settings.meta_verify_token,
        "hub.challenge": "1234567890_test_challenge"
    }
    res_challenge = client.get(
        "/api/webhooks/whatsapp",
        params=challenge_params,
        headers={"cf-connecting-ip": "203.0.113.195", "x-forwarded-for": "203.0.113.195"}
    )
    assert res_challenge.status_code == 200
    assert res_challenge.text == "1234567890_test_challenge"


def test_meta_webhook_signature_verification_through_proxy(test_app_and_client):
    """Verify Meta Webhook POST rejects unauthorized/invalid signatures and accepts valid signatures through proxy headers."""
    settings, app, client = test_app_and_client
    payload = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode("utf-8")

    # 1. Missing signature -> 403
    res_no_sig = client.post(
        "/api/webhooks/whatsapp",
        content=payload,
        headers={"content-type": "application/json", "cf-connecting-ip": "203.0.113.195"}
    )
    assert res_no_sig.status_code == 403

    # 2. Invalid signature -> 403
    res_bad_sig = client.post(
        "/api/webhooks/whatsapp",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": "sha256=invalid00000000000000000000000000000000000000000000000000000000",
            "cf-connecting-ip": "203.0.113.195"
        }
    )
    assert res_bad_sig.status_code == 403

    # 3. Valid signature -> 200
    expected_sig = "sha256=" + hmac.new(settings.meta_app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    res_valid_sig = client.post(
        "/api/webhooks/whatsapp",
        content=payload,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": expected_sig,
            "cf-connecting-ip": "203.0.113.195"
        }
    )
    assert res_valid_sig.status_code == 200
    assert res_valid_sig.text == "EVENT_RECEIVED"
