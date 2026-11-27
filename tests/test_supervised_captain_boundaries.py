"""What the supervised captain's own process tree may do: its scoped token reaches only the methods
the captain CLI uses, so it can never answer the user's own decisions; and it stops first."""

import contextlib
import threading
import time
from unittest.mock import patch

from home_isolation import SessionCase

from captain_barbossa import captain_bridge, launch, runtime, supervision, supervisor


class CaptainBoundaryTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.chdir(self.project))
        self.current = supervisor.open_session(self.project, None)
        self.supervisor = supervisor.Supervisor(self.current, "full-token", threading.Event())

    def answer(self, method, token, **params):
        return supervisor.Handler.answer(self.supervisor, method, {"token": token, **params})

    def test_the_captain_token_reaches_only_the_cli_methods(self):
        scoped = self.supervisor.captain_token
        self.assertNotEqual(scoped, self.supervisor.token)
        self.assertEqual(self.answer("state", scoped, crew="jack")["result"]["state"], None)
        for method in (
            "answer_captain",
            "resolve_authorization",
            "resolve_grant",
            "resolve_captain_delivery",
            "command",
            "sign_in",
            "shutdown",
        ):
            with self.subTest(method=method):
                error = self.answer(method, scoped)["error"]
                self.assertEqual(error["data"], {"code": "supervisor-captain-token"})
        user = self.answer("resolve_grant", self.supervisor.token, request="r")["error"]
        self.assertNotEqual(user["data"]["code"], "supervisor-captain-token")
        self.assertEqual(self.answer("state", "guess")["error"]["data"]["code"], "supervisor-token")

    def test_the_captain_is_launched_with_its_scoped_token_only(self):
        with (
            patch.object(launch, "prepare_launch", return_value=(["/bin/codex"], {})),
            patch.object(runtime, "executable", return_value="/bin/codex"),
        ):
            _, env, _, _ = supervisor.captain_command(self.supervisor, "codex", None, None)
        self.assertEqual(env[supervision.TOKEN_ENV], self.supervisor.captain_token)
        self.assertNotIn(self.supervisor.token, env.values())

    def test_an_uncertain_delivery_is_resolved_by_the_user_after_the_bridge_step(self):
        delivery = {"delivery_id": "d1", "crew": "jack", "status": "done", "summary": "ok"}
        captain_bridge.claim(self.current.directory, delivery, set())
        finished = threading.Event()

        def resolve():
            self.answer(
                "resolve_captain_delivery", self.supervisor.token, delivery_id="d1", decision="ack"
            )
            finished.set()

        with self.supervisor.bridge_lock:
            threading.Thread(target=resolve, daemon=True).start()
            time.sleep(0.2)
            self.assertFalse(finished.is_set())
        self.assertTrue(finished.wait(5))
        self.assertEqual(captain_bridge.claim(self.current.directory, delivery, set()), "ack")
        refused = self.answer(
            "resolve_captain_delivery", self.supervisor.token, delivery_id="d1", decision="maybe"
        )
        self.assertEqual(refused["error"]["data"]["code"], "captain-delivery-decision")

    def test_shutdown_stops_the_captain_before_the_crew(self):
        order = []
        with (
            patch.object(
                supervisor, "stop_captain", side_effect=lambda sup: order.append("captain")
            ),
            patch.object(
                supervisor, "stop_all", side_effect=lambda sup: order.append("crew") or []
            ),
        ):
            supervisor.shutdown(self.supervisor, {})
        self.assertEqual(order, ["captain", "crew"])
