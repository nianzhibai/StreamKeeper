import asyncio
import base64
import hashlib
import json
import time
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from stream_keeper.cloud.yun139 import Yun139Client
from stream_keeper.cloud.yun139_protocol import yun139_signature
from stream_keeper.web.cloud_login import (
    BaiduOpenListAuth,
    CloudLoginError,
    CloudLoginManager,
    CloudLoginPoll,
    GuangYaQrLoginFlow,
    Pan115QrLoginFlow,
    QuarkQrLoginFlow,
    WoPanQrLoginFlow,
    Yun139QrLoginFlow,
    create_cloud_login_flow,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\nvalid-enough-for-protocol-test"


class CloudLoginFlowTests(IsolatedAsyncioTestCase):
    async def test_quark_exchanges_ticket_for_cookie_without_exposing_ticket(self) -> None:
        ticket_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal ticket_calls
            if request.url.path.endswith("/getTokenForQrcodeLogin"):
                self.assertEqual(request.url.params["client_id"], "532")
                return httpx.Response(
                    200,
                    json={
                        "status": 2_000_000,
                        "message": "ok",
                        "data": {"members": {"token": "temporary-qr-token"}},
                    },
                    headers={"set-cookie": "_UP_A4A_11_=device; Domain=.quark.cn; Path=/"},
                )
            if request.url.path.endswith("/getServiceTicketByQrcodeToken"):
                ticket_calls += 1
                if ticket_calls == 1:
                    return httpx.Response(200, json={"status": 50_004_001, "message": "pending"})
                return httpx.Response(
                    200,
                    json={
                        "status": 2_000_000,
                        "data": {"members": {"service_ticket": "one-time-service-ticket"}},
                    },
                )
            if request.url.path == "/account/info":
                self.assertEqual(request.url.params["st"], "one-time-service-ticket")
                self.assertEqual(request.url.params["lw"], "scan")
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"nickname": "tester"}},
                    headers=[
                        ("set-cookie", "__pus=session-cookie; Domain=.quark.cn; Path=/; HttpOnly"),
                        ("set-cookie", "__puus=refresh-cookie; Domain=.quark.cn; Path=/; HttpOnly"),
                    ],
                )
            raise AssertionError(f"unexpected request: {request.url}")

        flow = QuarkQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            qr_image = await flow.start()
            self.assertTrue(qr_image.startswith("data:image/svg+xml;base64,"))
            svg = base64.b64decode(qr_image.partition(",")[2])
            self.assertIn(b"<svg", svg)
            self.assertEqual((await flow.poll()).state, "waiting")
            completed = await flow.poll()
            self.assertEqual(completed.state, "success")
            cookie = completed.credentials["cookie"] if completed.credentials else ""
            self.assertIn("__pus=session-cookie", cookie)
            self.assertIn("__puus=refresh-cookie", cookie)
            self.assertNotIn("one-time-service-ticket", cookie)
        finally:
            await flow.aclose()


    async def test_pan115_qr_login_returns_cookie_credentials(self) -> None:
        status_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal status_calls
            if request.url.path.endswith("/api/1.0/web/1.0/token"):
                return httpx.Response(
                    200,
                    json={
                        "state": 1,
                        "data": {"qrcode": "115-qr-content", "sign": "sign", "time": 123, "uid": "uid"},
                    },
                )
            if request.url.path.endswith("/get/status/"):
                status_calls += 1
                self.assertEqual(set(request.url.params), {"uid", "time", "sign", "_"})
                self.assertEqual(request.url.params["uid"], "uid")
                self.assertEqual(request.url.params["time"], "123")
                self.assertEqual(request.url.params["sign"], "sign")
                return httpx.Response(200, json={"state": 1, "data": {"status": 2}})
            if request.url.path.endswith("/login/qrcode"):
                return httpx.Response(
                    200,
                    json={
                        "state": 1,
                        "data": {"cookie": {"UID": "uid-value", "CID": "cid-value", "SEID": "seid-value"}},
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        flow = Pan115QrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            self.assertTrue((await flow.start()).startswith("data:image/svg+xml;base64,"))
            completed = await flow.poll()
            self.assertEqual(completed.state, "success")
            self.assertEqual(
                completed.credentials,
                {"cookie": "UID=uid-value; CID=cid-value; SEID=seid-value"},
            )
            self.assertEqual(status_calls, 1)
        finally:
            await flow.aclose()

    async def test_pan115_maps_waiting_scanned_expired_and_cancelled_states(self) -> None:
        statuses = iter((0, 1, -1, -2))

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/1.0/web/1.0/token"):
                return httpx.Response(
                    200,
                    json={
                        "state": 1,
                        "data": {"qrcode": "115-qr-content", "sign": "sign", "time": 123, "uid": "uid"},
                    },
                )
            if request.url.path.endswith("/get/status/"):
                return httpx.Response(200, json={"state": 1, "data": {"status": next(statuses)}})
            raise AssertionError(f"unexpected request: {request.url}")

        flow = Pan115QrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            await flow.start()
            self.assertEqual((await flow.poll()).state, "waiting")
            self.assertEqual((await flow.poll()).state, "scanned")
            self.assertEqual((await flow.poll()).state, "expired")
            self.assertEqual((await flow.poll()).state, "cancelled")
        finally:
            await flow.aclose()

    async def test_guangya_qr_login_returns_tokens(self) -> None:
        poll_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_calls
            if request.url.path.endswith("/device/code"):
                body = json.loads(request.content)
                self.assertEqual(body, {"scope": "user", "client_id": "aMe-8VSlkrbQXpUR"})
                return httpx.Response(
                    200,
                    json={
                        "device_code": "device-code",
                        "user_code": "user-code",
                        "expires_in": 120,
                        "interval": 2,
                        "verification_uri_complete": "https://account.guangyapan.com/__/auth/device/?client_id=x&user_code=user-code",
                    },
                )
            if request.url.path.endswith("/token"):
                body = json.loads(request.content)
                self.assertEqual(body["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")
                self.assertEqual(body["device_code"], "device-code")
                poll_calls += 1
                if poll_calls == 1:
                    return httpx.Response(400, json={"error": "authorization_pending", "error_code": 4050})
                return httpx.Response(
                    200,
                    json={
                        "token_type": "Bearer",
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "sub": "user-1",
                        "expires_in": 7200,
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        flow = GuangYaQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            qr_image = await flow.start()
            self.assertTrue(qr_image.startswith("data:image/svg+xml;base64,"))
            self.assertEqual((await flow.poll()).state, "waiting")
            completed = await flow.poll()
            self.assertEqual(completed.state, "success")
            self.assertEqual(
                completed.credentials,
                {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "client_id": "aMe-8VSlkrbQXpUR",
                },
            )
        finally:
            await flow.aclose()

    async def test_guangya_qr_login_maps_expired_and_error_states(self) -> None:
        responses = iter(
            (
                {"error": "expired_token", "error_code": 4052},
                {"error": "access_denied"},
            )
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/device/code"):
                return httpx.Response(
                    200,
                    json={
                        "device_code": "device-code",
                        "verification_uri_complete": "https://account.guangyapan.com/__/auth/device/",
                    },
                )
            if request.url.path.endswith("/token"):
                payload = next(responses)
                code = 400 if payload.get("error") == "expired_token" else 403
                return httpx.Response(code, json=payload)
            raise AssertionError(f"unexpected request: {request.url}")

        flow = GuangYaQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            await flow.start()
            self.assertEqual((await flow.poll()).state, "expired")
            with self.assertRaisesRegex(Exception, "扫码确认失败"):
                await flow.poll()
        finally:
            await flow.aclose()

    async def test_wopan_tracks_scanned_state_and_returns_both_tokens(self) -> None:
        query_calls = 0
        png = base64.b64encode(_PNG_BYTES).decode()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal query_calls
            self.assertEqual(request.headers["client-id"], "1001000021")
            if request.url.path.endswith("/QRCode/generate"):
                return httpx.Response(
                    200,
                    json={
                        "meta": {"code": "200", "message": "请求成功"},
                        "result": {"image": png, "uuid": "qr-session-uuid"},
                    },
                )
            if request.url.path.endswith("/QRCode/query"):
                self.assertEqual(request.url.params["uuid"], "qr-session-uuid")
                query_calls += 1
                if query_calls == 1:
                    result = {"state": 2, "token": None, "refreshToken": None}
                else:
                    result = {
                        "state": 3,
                        "token": "access-token-1234567890",
                        "refreshToken": "refresh-token-1234567890",
                    }
                return httpx.Response(200, json={"meta": {"code": "200"}, "result": result})
            raise AssertionError(f"unexpected request: {request.url}")

        flow = WoPanQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(await flow.start(), f"data:image/png;base64,{png}")
            self.assertEqual((await flow.poll()).state, "scanned")
            completed = await flow.poll()
            self.assertEqual(
                completed.credentials,
                {
                    "access_token": "access-token-1234567890",
                    "refresh_token": "refresh-token-1234567890",
                },
            )
        finally:
            await flow.aclose()


class Yun139QrLoginTests(IsolatedAsyncioTestCase):
    @staticmethod
    def encrypted_response(payload: dict, *, inner: bool = False) -> httpx.Response:
        payload = dict(payload)
        if inner:
            payload["data"] = AES.new(b"qPqDw263XgFgL3u8", AES.MODE_ECB).encrypt(
                pad(json.dumps(payload["data"]).encode(), 16),
            ).hex()
        iv = b"0123456789abcdef"
        ciphertext = AES.new(b"UqEZkrjCKfa02pP6jntzFmkzOz86zHUC", AES.MODE_CBC, iv=iv).encrypt(
            pad(json.dumps(payload).encode(), 16),
        )
        return httpx.Response(200, content=base64.b64encode(iv + ciphertext))

    async def test_qr_payload_device_binding_and_encrypted_polling_return_upload_credentials(self) -> None:
        account = "13800000000"
        token = f"login-token|a|b|{int((time.time() + 30 * 86400) * 1000)}"
        states = iter(("200059541", "200059548", "0000"))
        qr_contents = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://user-njs.yun.139.com/user/thirdlogin")
            encrypted = base64.b64decode(request.content)
            text = unpad(
                AES.new(b"UqEZkrjCKfa02pP6jntzFmkzOz86zHUC", AES.MODE_CBC, iv=encrypted[:16]).decrypt(
                    encrypted[16:],
                ), 16,
            ).decode()
            body = json.loads(text)
            query = parse_qs(urlsplit(qr_contents[0]).fragment.split("?", 1)[1])
            self.assertEqual(body["dycpwd"], query["sID"][0])
            self.assertIn(query["dID"][0], request.headers["X-Yun-Client-Info"])
            self.assertEqual(query["cType"], ["9"])
            self.assertEqual(body["msisdn"], "")
            self.assertEqual(body["pintype"], 21)
            self.assertEqual(body["clienttype"], 670)
            self.assertEqual(body["cpid"], 292)
            self.assertEqual(
                body["secinfo"], hashlib.sha1(f"fetion.com.cn:{body['dycpwd']}".encode()).hexdigest().upper(),
            )
            timestamp, nonce, signature = request.headers["Mcloud-Sign"].split(",")
            self.assertEqual(signature, yun139_signature(text, timestamp, nonce))
            self.assertNotIn("Authorization", request.headers)
            code = next(states)
            if code == "0000":
                return self.encrypted_response({"code": code, "data": {"account": account, "token": token}}, inner=True)
            return self.encrypted_response({"success": False, "code": code, "data": None})

        def render(content: str) -> str:
            qr_contents.append(content)
            return "data:image/svg+xml;base64,cXI="

        flow = Yun139QrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            with patch("stream_keeper.web.cloud_login._qr_svg_data_uri", side_effect=render):
                self.assertTrue((await flow.start()).startswith("data:image/"))
            self.assertTrue(qr_contents[0].startswith("https://yun.139.com/w/#/qrcLogin?"))
            self.assertEqual((await flow.poll()).state, "waiting")
            self.assertEqual((await flow.poll()).state, "scanned")
            completed = await flow.poll()
            self.assertEqual(completed.state, "success")
            expected = base64.b64encode(f"pc:{account}:{token}".encode()).decode()
            self.assertEqual(completed.credentials, {"authorization": expected})
            # The login output is the exact credential shape consumed by native uploads.
            uploader = Yun139Client(completed.credentials["authorization"])
            try:
                self.assertEqual(uploader.account, account)
            finally:
                await uploader.aclose()
        finally:
            await flow.aclose()

    async def test_expired_cancelled_and_rejected_states(self) -> None:
        codes = iter(("200059542", "200059549", "200059543"))
        flow = Yun139QrLoginFlow(transport=httpx.MockTransport(
            lambda _request: self.encrypted_response({"success": False, "code": next(codes)}),
        ))
        try:
            await flow.start()
            self.assertEqual((await flow.poll()).state, "expired")
            self.assertEqual((await flow.poll()).state, "cancelled")
            with self.assertRaisesRegex(CloudLoginError, "扫码确认失败"):
                await flow.poll()
        finally:
            await flow.aclose()

    async def test_invalid_encryption_and_incomplete_credentials_cannot_succeed(self) -> None:
        for response in (
            httpx.Response(200, content="not-valid-base64"),
            httpx.Response(200, json={"code": "0000", "data": {"account": "13800000000"}}),
            self.encrypted_response({"code": "0000", "data": "not-valid-hex"}),
            self.encrypted_response(
                {"code": "0000", "data": {"account": "13800000000", "token": "invalid"}}, inner=True,
            ),
            self.encrypted_response(
                {"code": "0000", "data": {"account": "13800000000", "token": "old|a|b|1"}}, inner=True,
            ),
            httpx.Response(503, text="private-upstream-response"),
        ):
            with self.subTest(response=response):
                flow = Yun139QrLoginFlow(transport=httpx.MockTransport(lambda _request, response=response: response))
                try:
                    await flow.start()
                    with self.assertRaises(CloudLoginError) as raised:
                        await flow.poll()
                    self.assertNotIn("private-upstream-response", str(raised.exception))
                finally:
                    await flow.aclose()

    async def test_poll_requires_start_and_transport_errors_are_sanitized(self) -> None:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("private-network-detail", request=request)

        flow = Yun139QrLoginFlow(transport=httpx.MockTransport(fail))
        try:
            with self.assertRaisesRegex(CloudLoginError, "尚未初始化"):
                await flow.poll()
            image = await flow.start()
            self.assertTrue(image.startswith("data:image/svg+xml;base64,"))
            with self.assertRaisesRegex(CloudLoginError, "扫码服务连接失败"):
                await flow.poll()
        finally:
            await flow.aclose()

    async def test_factory_registers_mobile_cloud(self) -> None:
        flow = create_cloud_login_flow("yun139")
        try:
            self.assertIsInstance(flow, Yun139QrLoginFlow)
        finally:
            await flow.aclose()


class BaiduOpenListAuthTests(IsolatedAsyncioTestCase):
    async def test_oob_authorization_exchanges_code_for_baidu_tokens(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/baiduyun/requests":
                self.assertEqual(request.url.params["driver_txt"], "baiduyun_ob")
                self.assertEqual(request.url.params["server_use"], "false")
                return httpx.Response(
                    200,
                    json={
                        "text": "https://openapi.baidu.com/oauth/2.0/authorize?response_type=code&redirect_uri=oob"
                    },
                )
            if request.url.path == "/baiduyun/callback":
                self.assertEqual(request.url.params["server_oob"], "true")
                self.assertEqual(request.url.params["code"], "baidu-authorization-code")
                encoded = base64.b64encode(
                    json.dumps(
                        {"access_token": "openlist-access-token", "refresh_token": "openlist-refresh-token"}
                    ).encode()
                ).decode()
                return httpx.Response(302, headers={"location": f"/#{encoded}"})
            raise AssertionError(f"unexpected request: {request.url}")

        auth = BaiduOpenListAuth(
            api_base="https://api.oplist.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            authorization_url = await auth.authorization_url()
            self.assertTrue(authorization_url.startswith("https://openapi.baidu.com/oauth/2.0/authorize"))
            credentials = await auth.exchange("baidu-authorization-code")
        finally:
            await auth.aclose()

        self.assertEqual(credentials["access_token"], "openlist-access-token")
        self.assertEqual(credentials["refresh_token"], "openlist-refresh-token")
        self.assertTrue(credentials["client_id"])
        self.assertTrue(credentials["client_secret"])

    async def test_untrusted_authorization_url_is_rejected(self) -> None:
        auth = BaiduOpenListAuth(
            api_base="https://api.oplist.test",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"text": "https://attacker.example/authorize"})
            ),
        )
        try:
            with self.assertRaisesRegex(CloudLoginError, "不可信"):
                await auth.authorization_url()
        finally:
            await auth.aclose()


class _RemoteUrlFlow:
    """A flow that regressed to returning a remote image URL instead of a data URI."""

    qr_ttl_seconds = 30

    def __init__(self, provider: str) -> None:
        self.closed = False

    async def start(self) -> str:
        return "https://qr.example.com/rendered-qrcode.png"

    async def poll(self) -> CloudLoginPoll:
        raise AssertionError("poll must not run when start() breaks the data URI contract")

    async def aclose(self) -> None:
        self.closed = True


class _SuccessfulFlow:
    qr_ttl_seconds = 30

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.closed = False

    async def start(self) -> str:
        return "data:image/png;base64,cXJjb2Rl"

    async def poll(self) -> CloudLoginPoll:
        if self.provider in {"quark", "pan115"}:
            return CloudLoginPoll("success", {"cookie": "cookie-secret"})
        if self.provider == "guangya":
            return CloudLoginPoll(
                "success",
                {"access_token": "access-token-123456", "refresh_token": "refresh-token", "client_id": "client-id"},
            )
        return CloudLoginPoll(
            "success",
            {"access_token": "access-token-123456", "refresh_token": "refresh-token"},
        )

    async def aclose(self) -> None:
        self.closed = True


class CloudLoginManagerTests(IsolatedAsyncioTestCase):
    async def test_manager_rejects_a_flow_that_returns_a_remote_qr_url(self) -> None:
        """The page CSP only renders `data:` images, so a remote URL must fail loudly here."""
        flows: list[_RemoteUrlFlow] = []

        async def save(provider: str, credentials: dict[str, str]) -> None:
            self.fail("credentials must not be saved")

        def create_flow(provider: str) -> _RemoteUrlFlow:
            flow = _RemoteUrlFlow(provider)
            flows.append(flow)
            return flow

        manager = CloudLoginManager(save, flow_factory=create_flow, poll_interval=0.01)
        try:
            with self.assertRaises(CloudLoginError):
                await manager.start("quark")
            self.assertTrue(flows[0].closed)
        finally:
            await manager.shutdown()

    async def test_manager_saves_credentials_in_background_and_hides_them_from_status(self) -> None:
        saved: list[tuple[str, dict[str, str]]] = []
        flows: list[_SuccessfulFlow] = []

        async def save(provider: str, credentials: dict[str, str]) -> None:
            saved.append((provider, dict(credentials)))

        def create_flow(provider: str) -> _SuccessfulFlow:
            flow = _SuccessfulFlow(provider)
            flows.append(flow)
            return flow

        manager = CloudLoginManager(save, flow_factory=create_flow, poll_interval=0.01)
        try:
            created = await manager.start("quark")
            self.assertEqual(created.state, "waiting")
            for _ in range(100):
                current = await manager.get("quark", created.session_id)
                if current and current.state == "success":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("QR login did not complete")
            self.assertEqual(saved, [("quark", {"cookie": "cookie-secret"})])
            self.assertIsNone(current.qr_image)
            self.assertNotIn("cookie-secret", repr(current))
            self.assertTrue(flows[0].closed)

            saved.clear()
            created = await manager.start("pan115")
            for _ in range(100):
                current = await manager.get("pan115", created.session_id)
                if current and current.state == "success":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("115 QR login did not complete")
            self.assertEqual(saved, [("pan115", {"cookie": "cookie-secret"})])
            self.assertTrue(flows[1].closed)

            saved.clear()
            created = await manager.start("guangya")
            for _ in range(100):
                current = await manager.get("guangya", created.session_id)
                if current and current.state == "success":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("GuangYa QR login did not complete")
            self.assertEqual(
                saved,
                [
                    (
                        "guangya",
                        {
                            "access_token": "access-token-123456",
                            "refresh_token": "refresh-token",
                            "client_id": "client-id",
                        },
                    )
                ],
            )
            self.assertTrue(flows[2].closed)
        finally:
            await manager.shutdown()
