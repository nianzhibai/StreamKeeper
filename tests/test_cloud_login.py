import asyncio
import base64
import json
from unittest import IsolatedAsyncioTestCase

import httpx

from stream_keeper.web.cloud_login import (
    BaiduQrLoginFlow,
    CloudLoginManager,
    CloudLoginPoll,
    GuangYaQrLoginFlow,
    Pan115QrLoginFlow,
    QuarkQrLoginFlow,
    WoPanQrLoginFlow,
)


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

    async def test_baidu_qr_login_returns_cookie_credentials(self) -> None:
        poll_calls = 0
        home_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal poll_calls, home_calls
            if request.url.path.endswith("/v2/api/getqrcode"):
                self.assertEqual(request.url.params["tpl"], "netdisk")
                self.assertEqual(request.url.params["lp"], "pc")
                self.assertEqual(request.url.params["apiver"], "v3")
                return httpx.Response(
                    200,
                    json={
                        "errno": 0,
                        "sign": "qr-sign",
                        "imgurl": "passport.baidu.com/v2/api/qrcode?sign=qr-sign&lp=pc",
                    },
                )
            if request.url.path.endswith("/channel/unicast"):
                poll_calls += 1
                if poll_calls == 1:
                    return httpx.Response(200, json={"errno": 1})
                if poll_calls == 2:
                    return httpx.Response(200, json={"errno": 1})
                return httpx.Response(
                    200,
                    json={"errno": 0, "channel_v": '{"status": 0, "v": "login-bduss", "u": ""}'},
                )
            if request.url.path.endswith("/v3/login/main/qrbdusslogin"):
                self.assertEqual(request.url.params["bduss"], "login-bduss")
                self.assertEqual(request.url.params["loginVersion"], "v5")
                return httpx.Response(
                    200,
                    json={"errInfo": {"no": "0"}, "code": "110000", "data": {"u": "https://pan.baidu.com/disk/home"}},
                    headers=[
                        ("set-cookie", "BDUSS=baidu-session-cookie; Domain=baidu.com; Path=/"),
                        ("set-cookie", "STOKEN=passport-stoken; Domain=passport.baidu.com; Path=/"),
                    ],
                )
            if request.url.path == "/disk/home":
                home_calls += 1
                return httpx.Response(
                    302,
                    headers={
                        "location": "/disk/main?from=homeFlow",
                        "set-cookie": "STOKEN=netdisk-stoken; Domain=.pan.baidu.com; Path=/; HttpOnly",
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        flow = BaiduQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            qr_image = await flow.start()
            self.assertEqual(qr_image, "https://passport.baidu.com/v2/api/qrcode?sign=qr-sign&lp=pc")
            self.assertEqual((await flow.poll()).state, "waiting")
            self.assertEqual((await flow.poll()).state, "waiting")
            completed = await flow.poll()
            self.assertEqual(completed.state, "success")
            cookie = completed.credentials["cookie"] if completed.credentials else ""
            self.assertIn("BDUSS=baidu-session-cookie", cookie)
            self.assertIn("STOKEN=netdisk-stoken", cookie)
            self.assertNotIn("login-bduss", cookie)
            self.assertNotIn("passport-stoken", cookie)
            self.assertEqual(home_calls, 1)
        finally:
            await flow.aclose()

    async def test_baidu_qr_login_maps_scanned_and_cancelled_states(self) -> None:
        statuses = iter(('{"status": 1}', '{"status": 2}'))

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v2/api/getqrcode"):
                return httpx.Response(
                    200,
                    json={"errno": 0, "sign": "qr-sign", "imgurl": "passport.baidu.com/v2/api/qrcode"},
                )
            if request.url.path.endswith("/channel/unicast"):
                return httpx.Response(200, json={"errno": 0, "channel_v": next(statuses)})
            raise AssertionError(f"unexpected request: {request.url}")

        flow = BaiduQrLoginFlow(transport=httpx.MockTransport(handler))
        try:
            await flow.start()
            self.assertEqual((await flow.poll()).state, "scanned")
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
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nvalid-enough-for-protocol-test").decode()

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


class _SuccessfulFlow:
    qr_ttl_seconds = 30

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.closed = False

    async def start(self) -> str:
        return "data:image/png;base64,cXJjb2Rl"

    async def poll(self) -> CloudLoginPoll:
        if self.provider in {"quark", "baidu"}:
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
            created = await manager.start("baidu")
            for _ in range(100):
                current = await manager.get("baidu", created.session_id)
                if current and current.state == "success":
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Baidu QR login did not complete")
            self.assertEqual(saved, [("baidu", {"cookie": "cookie-secret"})])
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
