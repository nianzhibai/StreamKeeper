import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import httpx

from douyin_recorder.cloud import QuarkClient


async def no_sleep(_seconds: float) -> None:
    return None


class QuarkClientTests(IsolatedAsyncioTestCase):
    async def test_native_multipart_upload_verifies_and_updates_cookie(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"0": []}
        uploaded_parts: dict[int, bytes] = {}
        credential_updates: list[dict[str, str]] = []
        created_counter = 0
        upload_parent = ""
        upload_name = ""
        upload_size = 0
        finished = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal created_counter, upload_parent, upload_name, upload_size, finished
            if request.url.host == "bucket.oss.example":
                body = await request.aread()
                if request.method == "PUT":
                    part_number = int(request.url.params["partNumber"])
                    uploaded_parts[part_number] = body
                    self.assertEqual(request.headers["content-length"], str(len(body)))
                    return httpx.Response(200, headers={"ETag": f'"etag-{part_number}"'})
                self.assertEqual(request.method, "POST")
                self.assertIn(b"<PartNumber>1</PartNumber>", body)
                self.assertIn(b"<PartNumber>2</PartNumber>", body)
                expected_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
                self.assertEqual(request.headers["content-md5"], expected_md5)
                return httpx.Response(200, json={"ok": True})

            self.assertEqual(request.url.host, "drive.quark.cn")
            self.assertEqual(request.url.params["pr"], "ucpro")
            self.assertEqual(request.url.params["fr"], "pc")
            self.assertIn("old-cookie", request.headers["cookie"])
            path = request.url.path.removeprefix("/1/clouddrive")
            headers = {"Set-Cookie": "__puus=rotated-cookie; Path=/; Secure"} if path == "/file/sort" else None
            if path == "/file/sort":
                parent = request.url.params["pdir_fid"]
                entries = directories.get(parent, [])
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "status": 200,
                        "code": 0,
                        "data": {"list": entries},
                        "metadata": {"_total": len(entries)},
                    },
                )

            body = json.loads(await request.aread())
            if path == "/file":
                created_counter += 1
                directory_id = f"directory-{created_counter}"
                directories.setdefault(body["pdir_fid"], []).append(
                    {
                        "fid": directory_id,
                        "file_name": body["file_name"],
                        "size": 0,
                        "file": False,
                    }
                )
                directories[directory_id] = []
                return httpx.Response(200, json={"status": 200, "code": 0, "data": {"fid": directory_id}})
            if path == "/file/upload/pre":
                upload_parent = body["pdir_fid"]
                upload_name = body["file_name"]
                upload_size = body["size"]
                return httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "code": 0,
                        "data": {
                            "task_id": "task-1",
                            "upload_id": "upload-1",
                            "obj_key": "object-key",
                            "upload_url": "https://oss.example",
                            "bucket": "bucket",
                            "auth_info": "auth-info",
                            "callback": {"callbackUrl": "https://callback", "callbackBody": "body"},
                        },
                        "metadata": {"part_size": 4},
                    },
                )
            if path == "/file/update/hash":
                self.assertEqual(body["md5"], hashlib.md5(b"abcdef").hexdigest())
                self.assertEqual(body["sha1"], hashlib.sha1(b"abcdef").hexdigest())
                return httpx.Response(200, json={"status": 200, "code": 0, "data": {"finish": False}})
            if path == "/file/upload/auth":
                return httpx.Response(200, json={"status": 200, "code": 0, "data": {"auth_key": "key"}})
            if path == "/file/upload/finish":
                finished = True
                directories[upload_parent].append(
                    {"fid": "file-1", "file_name": upload_name, "size": upload_size, "file": True}
                )
                return httpx.Response(200, json={"status": 200, "code": 0})
            return httpx.Response(404, json={"status": 404, "code": 1, "message": "not found"})

        async def save_credentials(state: dict[str, str]) -> None:
            credential_updates.append(state)

        transport = httpx.MockTransport(handler)
        with TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "local.ts"
            local_path.write_bytes(b"abcdef")
            client = QuarkClient(
                "session=old-cookie; __puus=old",
                on_credential_update=save_credentials,
                transport=transport,
                sleep=no_sleep,
            )
            try:
                remote_path = "/archive/主播/remote.ts"
                self.assertTrue(await client.upload_verified(local_path, remote_path))
                self.assertFalse(await client.upload_verified(local_path, remote_path))
            finally:
                await client.aclose()

        self.assertTrue(finished)
        self.assertEqual(upload_name, "remote.ts")
        self.assertEqual(uploaded_parts, {1: b"abcd", 2: b"ef"})
        self.assertEqual(credential_updates, [{"cookie": "session=old-cookie; __puus=rotated-cookie"}])
