import asyncio
import base64
from unittest import IsolatedAsyncioTestCase

import httpx

from stream_keeper.web.cloud_login import (
    CloudLoginManager,
    CloudLoginPoll,
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
        if self.provider == "quark":
            return CloudLoginPoll("success", {"cookie": "cookie-secret"})
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
        finally:
            await manager.shutdown()
