import unittest

import httpx

from stream_keeper.web.updates import LATEST_RELEASE_URL, UpdateChecker, UpdateCheckError


class UpdateCheckerTests(unittest.IsolatedAsyncioTestCase):
    async def test_newer_release_is_reported_and_cached(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"tag_name": "v1.4.0"})

        checker = UpdateChecker("1.3.2", transport=httpx.MockTransport(handle))

        first = await checker.check()
        second = await checker.check()

        self.assertTrue(first.update_available)
        self.assertEqual(first.current_version, "1.3.2")
        self.assertEqual(first.latest_version, "1.4.0")
        self.assertEqual(first.release_url, "https://github.com/nianzhibai/StreamKeeper/releases/tag/v1.4.0")
        self.assertIs(first, second)
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), LATEST_RELEASE_URL)
        self.assertEqual(requests[0].headers["User-Agent"], "StreamKeeper/1.3.2")

    async def test_equal_or_older_release_does_not_offer_an_update(self) -> None:
        for latest in ("v1.3.2", "v1.2.9"):
            with self.subTest(latest=latest):
                transport = httpx.MockTransport(
                    lambda _request, tag=latest: httpx.Response(200, json={"tag_name": tag}),
                )
                result = await UpdateChecker("1.3.2", transport=transport).check()
                self.assertFalse(result.update_available)

    async def test_invalid_release_version_is_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"tag_name": "latest"}),
        )

        with self.assertRaisesRegex(UpdateCheckError, "最新版本号格式无效"):
            await UpdateChecker("1.3.2", transport=transport).check()

    async def test_github_failure_is_converted_to_domain_error(self) -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(503))

        with self.assertRaisesRegex(UpdateCheckError, "无法连接 GitHub"):
            await UpdateChecker("1.3.2", transport=transport).check()
