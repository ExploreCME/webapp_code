import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from jinja2 import Environment, FileSystemLoader


TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "template standardization"

# Execute the rendered browser script with deterministic Clerk, cookie and storage doubles.
HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
let load, listener, signOutHandler, prematureLogout = false;
let reloads = 0, cookie = input.cookie || "", now = 100000;
const storage = new Map(Object.entries(input.storage || {}));
const state = id => id === "transition" ? { session: undefined, user: undefined } : ({
    session: id ? { getToken: async () => "fresh-" + id } : null,
    user: id ? { id, firstName: "Student" } : null,
});
const clerk = {
    ...state(input.user), load: async () => {},
    addListener: fn => { listener = fn; },
    signOut: async () => {
        await listener(state(null));
        prematureLogout = Boolean(context.window.location.href);
    },
};
const context = {
    Clerk: clerk, console, Date: { now: () => now },
    document: {
        get cookie() { return cookie; }, set cookie(value) { cookie = value; },
        getElementById: id => id === "sign-out-button" && input.clickSignOut ? {
            style: {}, addEventListener: (_, fn) => { signOutHandler = fn; },
        } : null,
    },
    sessionStorage: {
        getItem: key => {
            if (input.storageBlocked) throw Error("Storage blocked");
            return storage.get(key) || null;
        },
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key),
    },
    fetch: async () => ({}),
    window: {
        Clerk: clerk, addEventListener: (_, fn) => { load = fn; },
        location: { href: "", reload: () => { reloads++; } },
    },
};
(async () => {
    vm.runInNewContext(input.script, context);
    await load();
    if (input.clickSignOut) await signOutHandler({ preventDefault() {} });
    for (const event of input.events || []) {
        Object.assign(clerk, state(event.user));
        await listener(clerk);
        await Promise.resolve();
    }
    console.log(JSON.stringify({
        reloads, cookie, prematureLogout, storage: Object.fromEntries(storage),
        location: context.window.location.href,
    }));
})().catch(error => { console.error(error); process.exit(1); });
"""


class AuthBridgeTests(unittest.TestCase):
    def script(self, flask_user=None):
        env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
        html = env.get_template("base.html").render(
            g={"user_id": flask_user}, session={},
            url_for=lambda endpoint, **kwargs: "/" + endpoint,
            get_flashed_messages=lambda **kwargs: [],
        )
        return re.search(
            r"<!-- Clerk Initialization Script -->\s*<script>(.*?)</script>",
            html, re.S,
        ).group(1)

    def test_rendered_script_reconciles_server_identity_with_guard(self):
        script = self.script()
        self.assertIn("const flaskUserId = null;", script)
        self.assertIn('const reloadGuardKey = "paxcel-auth-reload";', script)
        self.assertIn("flaskUserId === user.id", script)
        self.assertIn("now - guard.at < 30000", script)
        self.assertIn("sessionStorage.setItem(reloadGuardKey", script)
        self.assertIn("sessionStorage.removeItem(reloadGuardKey)", script)
        self.assertIn("Clerk.addListener", script)
        self.assertNotIn("cookieExists", script)
        self.assertEqual(script.count("window.location.reload()"), 1)
        self.assertLess(script.index("sessionStorage.setItem"), script.index("window.location.reload()"))
        self.assertIn('const flaskUserId = "known-user";', self.script("known-user"))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the browser script")
    def test_auth_state_transitions(self):
        guard = json.dumps({"userId": "new-user", "at": 99999})
        scenarios = (
            ("new sign-in", {}, {"user": "new-user"}, 1, ""),
            ("stale cookie", {}, {"user": "new-user", "cookie": "__session=expired"}, 1, ""),
            ("repeat notifications", {}, {
                "user": "new-user", "events": [{"user": "new-user"}] * 3,
            }, 1, ""),
            ("failed server recovery", {}, {
                "user": "new-user", "storage": {"paxcel-auth-reload": guard},
            }, 0, ""),
            ("expired guard", {}, {
                "user": "new-user",
                "storage": {"paxcel-auth-reload": json.dumps({"userId": "new-user", "at": 1})},
            }, 1, ""),
            ("server recognized user", {"flask_user": "new-user"}, {
                "user": "new-user", "storage": {"paxcel-auth-reload": guard},
            }, 0, ""),
            ("account switch", {"flask_user": "old-user"}, {"user": "new-user"}, 1, ""),
            ("listener sign-in", {}, {"events": [{"user": "new-user"}]}, 1, ""),
            ("anonymous", {}, {}, 0, ""),
            ("listener sign-out", {"flask_user": "new-user"}, {
                "user": "new-user", "events": [{"user": None}],
            }, 0, "/logout"),
            ("Clerk already signed out", {"flask_user": "old-user"}, {}, 0, "/logout"),
            ("transitional session", {"flask_user": "new-user"}, {
                "user": "new-user", "events": [{"user": "transition"}, {"user": "new-user"}],
            }, 0, ""),
            ("explicit sign-out finishes first", {"flask_user": "new-user"}, {
                "user": "new-user", "clickSignOut": True,
            }, 0, "/logout"),
            ("blocked storage", {}, {"user": "new-user", "storageBlocked": True}, 0, ""),
        )
        for name, render_args, browser_args, reloads, location in scenarios:
            with self.subTest(name=name):
                result = subprocess.run(
                    ["node", "-e", HARNESS],
                    input=json.dumps({"script": self.script(**render_args), **browser_args}),
                    capture_output=True, text=True, check=True, timeout=10,
                )
                state = json.loads(result.stdout)
                self.assertEqual(state["reloads"], reloads)
                self.assertEqual(state["location"], location)
                self.assertFalse(state["prematureLogout"])
                if browser_args.get("user") and not location:
                    self.assertIn("__session=fresh-new-user;", state["cookie"])
                if name in ("server recognized user", "listener sign-out", "anonymous"):
                    self.assertNotIn("paxcel-auth-reload", state["storage"])


if __name__ == "__main__":
    unittest.main()
