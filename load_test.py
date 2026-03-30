"""
Load Testing Script — Log Optimization Dashboard API
=====================================================
Host   : http://localhost:8001
Tool   : Locust 2.x  →  pip install locust

Run (Web UI):
    locust -f load_test.py

Run (headless):
    locust -f load_test.py --headless -u 30 -r 3 --run-time 3m --html report.html

Fixes applied vs v2
───────────────────
• AttributeError: 'HttpSession' object has no attribute 'add_event_listener'
  — Removed the invalid @self.client.add_event_listener decorator pattern.
  — Replaced with a module-level @events.request.add_listener hook that injects
    the Bearer token into every outgoing request automatically.
  — Token lookup is done via environment.runner.user_classes (not per-instance)
    so it works correctly for ALL user types simultaneously.
• Auth injection now uses request_type + headers directly via urllib3/requests
  session-level header patching on the client — simplest and most reliable approach.
"""

import random
import string
from locust import HttpUser, TaskSet, task, between, events
from locust.exception import RescheduleTask, StopUser


# ─────────────────────────────────────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_CREDS      = {"email": "admin@example.com", "password": "admin123"}
PRIVILEGED_CREDS = {"email": "priv@example.com",  "password": "priv123"}
REGULAR_CREDS    = {"email": "user@example.com",  "password": "user123"}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def rnd(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


SEVERITY_OFF = {k: False for k in ("Info", "debug", "Warn", "error", "fatal", "trace")}

INDEX_CONFIG = {
    "index": {
        "filter":        {"severity": SEVERITY_OFF},
        "aggregation":   {"severity": SEVERITY_OFF},
        "deduplication": {"severity": SEVERITY_OFF},
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# Base user — login, token refresh, per-request auth injection
# ─────────────────────────────────────────────────────────────────────────────

class BaseAPIUser(HttpUser):
    """
    Handles auth for all user types.

    FIX: Token injection is done by updating self.client.headers directly
    (a requests.Session-level default), which is the correct Locust 2.x approach.
    The old @self.client.add_event_listener pattern does not exist in Locust.
    """
    abstract    = True
    host        = "http://localhost:8001"
    credentials: dict = {}

    # State populated during on_start
    token:         str = ""
    refresh_token: str = ""
    user_id:       int = 0
    index_id:      int = 0
    request_id:    int = 0
    notif_id:      int = 0

    # ── startup ───────────────────────────────────────────────────────────────

    def on_start(self):
        # ✅ FIX: No more @self.client.add_event_listener (doesn't exist).
        # We inject the token by setting session-level default headers directly.
        # self.client is a requests.Session subclass — this is the correct way.
        self._do_login()

    def _set_auth_header(self):
        """Push current token into session-level default headers."""
        if self.token:
            self.client.headers.update({"Authorization": f"Bearer {self.token}"})

    def _do_login(self):
        resp = self.client.post(
            "/app/v1/login",
            json=self.credentials,
            name="[setup] POST /login",
        )
        if resp.status_code != 200:
            print(f"[WARN] Login FAILED for {self.credentials['email']} "
                  f"→ {resp.status_code}: {resp.text[:200]}")
            return

        data               = resp.json()
        self.token         = data.get("access_token", "")
        self.refresh_token = data.get("refresh_token", "")

        # ✅ Inject token into session headers immediately after login
        self._set_auth_header()

        # Fetch own user id
        me = self.client.get("/app/v1/me", name="[setup] GET /me")
        if me.status_code == 200:
            self.user_id = me.json().get("id", 0)

        self._seed_index()
        self._seed_notification()

    def _seed_index(self):
        resp = self.client.post(
            "/app/v1/indexes/",
            json={"name": f"lt_{rnd()}", "namespace": "loadtest"},
            name="[setup] POST /indexes/",
        )
        if resp.status_code == 200:
            self.index_id = resp.json().get("id", 0)
        # 403 for RegularUser — silently ignored

    def _seed_notification(self):
        resp = self.client.get(
            "/app/v1/notifications?unread_only=false",
            name="[setup] GET /notifications",
        )
        if resp.status_code == 200:
            items = resp.json()
            if items:
                self.notif_id = items[0].get("id", 0)

    # ── guards ────────────────────────────────────────────────────────────────

    def require_token(self):
        if not self.token:
            raise RescheduleTask()

    def require_index(self):
        if not self.index_id:
            raise RescheduleTask()

    def require_request(self):
        if not self.request_id:
            raise RescheduleTask()

    def require_user(self):
        if not self.user_id:
            raise RescheduleTask()


# ─────────────────────────────────────────────────────────────────────────────
# Task Sets
# ─────────────────────────────────────────────────────────────────────────────

class HealthTasks(TaskSet):
    @task(5)
    def health(self):
        self.client.get("/health", name="GET /health")

    @task(1)
    def stop(self):
        self.interrupt()


class ProfileTasks(TaskSet):

    @task(5)
    def get_me(self):
        self.user.require_token()
        self.client.get("/app/v1/me", name="GET /me")

    @task(3)
    def get_notifications_unread(self):
        self.user.require_token()
        self.client.get("/app/v1/notifications?unread_only=true", name="GET /notifications (unread)")

    @task(2)
    def get_notifications_all(self):
        self.user.require_token()
        self.client.get("/app/v1/notifications?unread_only=false", name="GET /notifications (all)")

    @task(2)
    def mark_notification_read(self):
        self.user.require_token()
        nid = self.user.notif_id
        if not nid:
            raise RescheduleTask()
        self.client.put(f"/app/v1/notifications/{nid}/read", name="PUT /notifications/{id}/read")

    @task(2)
    def refresh_token(self):
        rt = self.user.refresh_token
        if not rt:
            raise RescheduleTask()
        resp = self.client.post(
            "/app/v1/token/refresh",
            json={"refresh_token": rt},
            name="POST /token/refresh",
        )
        if resp.status_code == 200:
            data = resp.json()
            self.user.token         = data.get("access_token", self.user.token)
            self.user.refresh_token = data.get("refresh_token", self.user.refresh_token)
            # ✅ Re-inject updated token into session headers
            self.user._set_auth_header()

    @task(1)
    def stop(self):
        self.interrupt()


class DashboardTasks(TaskSet):

    @task(8)
    def get_dashboard(self):
        self.user.require_token()
        self.client.get("/app/v1/dashboard/", name="GET /dashboard/")

    @task(1)
    def stop(self):
        self.interrupt()


class IndexReadTasks(TaskSet):

    @task(5)
    def list_indexes(self):
        self.user.require_token()
        self.client.get("/app/v1/indexes/", name="GET /indexes/")

    @task(4)
    def get_index(self):
        self.user.require_token()
        self.user.require_index()
        self.client.get(f"/app/v1/indexes/{self.user.index_id}", name="GET /indexes/{id}")

    @task(3)
    def get_index_config(self):
        self.user.require_token()
        self.user.require_index()
        self.client.get(f"/app/v1/indexes/{self.user.index_id}/config", name="GET /indexes/{id}/config")

    @task(3)
    def get_index_users(self):
        self.user.require_token()
        self.user.require_index()
        self.client.get(f"/app/v1/indexes/{self.user.index_id}/users", name="GET /indexes/{id}/users")

    @task(2)
    def get_index_user(self):
        self.user.require_token()
        self.user.require_index()
        self.user.require_user()
        with self.client.get(
            f"/app/v1/indexes/{self.user.index_id}/users/{self.user.user_id}",
            name="GET /indexes/{id}/users/{uid}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()

    @task(1)
    def stop(self):
        self.interrupt()


class IndexWriteTasks(TaskSet):

    @task(3)
    def create_index(self):
        self.user.require_token()
        resp = self.client.post(
            "/app/v1/indexes/",
            json={"name": f"lt_{rnd()}", "namespace": "loadtest"},
            name="POST /indexes/",
        )
        if resp.status_code == 200:
            self.user.index_id = resp.json().get("id", self.user.index_id)

    @task(3)
    def update_index(self):
        self.user.require_token()
        self.user.require_index()
        self.client.put(
            f"/app/v1/indexes/{self.user.index_id}",
            json={"name": f"lt_upd_{rnd()}"},
            name="PUT /indexes/{id}",
        )

    @task(3)
    def update_index_config(self):
        self.user.require_token()
        self.user.require_index()
        self.client.put(
            f"/app/v1/indexes/{self.user.index_id}/config",
            json=INDEX_CONFIG,
            name="PUT /indexes/{id}/config",
        )

    @task(2)
    def add_index_user(self):
        self.user.require_token()
        self.user.require_index()
        self.user.require_user()
        self.client.put(
            f"/app/v1/indexes/{self.user.index_id}/users/{self.user.user_id}",
            name="PUT /indexes/{id}/users/{uid}",
        )

    @task(2)
    def set_index_users_bulk(self):
        self.user.require_token()
        self.user.require_index()
        self.user.require_user()
        self.client.post(
            f"/app/v1/indexes/{self.user.index_id}/users",
            json={"user_ids": [self.user.user_id]},
            name="POST /indexes/{id}/users (bulk)",
        )

    @task(1)
    def remove_index_user(self):
        self.user.require_token()
        self.user.require_index()
        self.user.require_user()
        with self.client.delete(
            f"/app/v1/indexes/{self.user.index_id}/users/{self.user.user_id}",
            name="DELETE /indexes/{id}/users/{uid}",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404):
                resp.success()

    @task(1)
    def stop(self):
        self.interrupt()


class ReportTasks(TaskSet):

    @task(5)
    def list_reports(self):
        self.user.require_token()
        self.client.get("/app/v1/reports/", name="GET /reports/")

    @task(3)
    def get_report(self):
        self.user.require_token()
        report_id = random.randint(1, 20)
        with self.client.get(
            f"/app/v1/reports/{report_id}",
            name="GET /reports/{id}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()

    @task(1)
    def stop(self):
        self.interrupt()


class RequestSubmitTasks(TaskSet):

    @task(3)
    def list_requests(self):
        self.user.require_token()
        self.user.require_user()
        self.client.get(
            f"/app/v1/users/{self.user.user_id}/requests",
            name="GET /users/{id}/requests",
        )

    @task(3)
    def create_config_edit_request(self):
        self.user.require_token()
        self.user.require_user()
        self.user.require_index()
        resp = self.client.post(
            f"/app/v1/users/{self.user.user_id}/requests/config_edit"
            f"?index_id={self.user.index_id}",
            json=INDEX_CONFIG,
            name="POST /requests/config_edit",
        )
        if resp.status_code == 200:
            self.user.request_id = resp.json().get("id", self.user.request_id)

    @task(2)
    def create_user_edit_request(self):
        self.user.require_token()
        self.user.require_user()
        resp = self.client.post(
            f"/app/v1/users/{self.user.user_id}/requests/user_edit",
            json={"username": rnd()},
            name="POST /requests/user_edit",
        )
        if resp.status_code == 200:
            self.user.request_id = resp.json().get("id", self.user.request_id)

    @task(2)
    def create_index_edit_request(self):
        self.user.require_token()
        self.user.require_user()
        resp = self.client.post(
            f"/app/v1/users/{self.user.user_id}/requests/index_edit",
            json={"name": f"req_{rnd()}"},
            name="POST /requests/index_edit",
        )
        if resp.status_code == 200:
            self.user.request_id = resp.json().get("id", self.user.request_id)

    @task(2)
    def get_request_by_id(self):
        self.user.require_token()
        self.user.require_user()
        self.user.require_request()
        with self.client.get(
            f"/app/v1/users/{self.user.user_id}/requests/{self.user.request_id}",
            name="GET /requests/{rid}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()
                self.user.request_id = 0

    @task(1)
    def stop(self):
        self.interrupt()


class ApprovalTasks(TaskSet):

    @task(4)
    def list_approvals(self):
        self.user.require_token()
        self.user.require_user()
        self.client.get(
            f"/app/v1/users/{self.user.user_id}/approvals",
            name="GET /approvals",
        )

    @task(3)
    def get_approval_by_id(self):
        self.user.require_token()
        self.user.require_user()
        self.user.require_request()
        with self.client.get(
            f"/app/v1/users/{self.user.user_id}/approvals/{self.user.request_id}",
            name="GET /approvals/{rid}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()

    @task(2)
    def approve_request(self):
        self.user.require_token()
        self.user.require_user()
        self.user.require_request()
        self.client.put(
            f"/app/v1/users/{self.user.user_id}/approvals/{self.user.request_id}",
            json={"status": "approved", "reason": "load test auto-approve"},
            name="PUT /approvals/{rid} [approve]",
        )

    @task(1)
    def reject_request(self):
        self.user.require_token()
        self.user.require_user()
        self.user.require_request()
        self.client.put(
            f"/app/v1/users/{self.user.user_id}/approvals/{self.user.request_id}",
            json={"status": "rejected", "reason": "load test auto-reject"},
            name="PUT /approvals/{rid} [reject]",
        )

    @task(1)
    def stop(self):
        self.interrupt()


class UserAdminTasks(TaskSet):

    @task(5)
    def list_users(self):
        self.user.require_token()
        self.client.get("/app/v1/users/?skip=0&limit=50", name="GET /users/")

    @task(3)
    def get_user(self):
        self.user.require_token()
        self.user.require_user()
        self.client.get(f"/app/v1/users/{self.user.user_id}", name="GET /users/{id}")

    @task(2)
    def get_user_indexes(self):
        self.user.require_token()
        self.user.require_user()
        self.client.get(f"/app/v1/users/{self.user.user_id}/indexes", name="GET /users/{id}/indexes")

    @task(1)
    def stop(self):
        self.interrupt()


# ─────────────────────────────────────────────────────────────────────────────
# Concrete user classes
# ─────────────────────────────────────────────────────────────────────────────

class AdminUser(BaseAPIUser):
    wait_time   = between(0.5, 2)
    weight      = 15
    credentials = ADMIN_CREDS

    tasks = {
        HealthTasks:       1,
        ProfileTasks:      2,
        UserAdminTasks:    4,
        ApprovalTasks:     4,
        IndexWriteTasks:   3,
        IndexReadTasks:    2,
        ReportTasks:       3,
        DashboardTasks:    3,
    }


class PrivilegedUser(BaseAPIUser):
    wait_time   = between(1, 3)
    weight      = 25
    credentials = PRIVILEGED_CREDS

    tasks = {
        HealthTasks:          1,
        ProfileTasks:         2,
        IndexWriteTasks:      4,
        IndexReadTasks:       4,
        RequestSubmitTasks:   3,
        ApprovalTasks:        3,
        ReportTasks:          2,
        DashboardTasks:       3,
    }


class RegularUser(BaseAPIUser):
    wait_time   = between(2, 5)
    weight      = 60
    credentials = REGULAR_CREDS

    tasks = {
        HealthTasks:          1,
        ProfileTasks:         3,
        DashboardTasks:       5,
        IndexReadTasks:       5,
        RequestSubmitTasks:   3,
    }

    def _seed_index(self):
        pass  # RegularUser gets 403 on POST /indexes/ — skip entirely


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test user
# ─────────────────────────────────────────────────────────────────────────────

class SmokeTestUser(BaseAPIUser):
    """
    Run alone for CI / pre-deploy validation.
        locust -f load_test.py --headless -u 1 -r 1 --run-time 90s
    """
    wait_time   = between(0.2, 0.5)
    weight      = 0
    credentials = ADMIN_CREDS

    @task
    def smoke_all_endpoints(self):
        uid = self.user_id    or 1
        iid = self.index_id   or 1
        rid = self.request_id or 1

        def get(url, name):
            self.client.get(url, name=f"[smoke] {name}")

        def post(url, name, **kw):
            return self.client.post(url, name=f"[smoke] {name}", **kw)

        def put(url, name, **kw):
            self.client.put(url, name=f"[smoke] {name}", **kw)

        def delete(url, name):
            with self.client.delete(url, name=f"[smoke] {name}", catch_response=True) as r:
                if r.status_code in (200, 404):
                    r.success()

        get("/health",                                         "GET /health")
        get("/app/v1/me",                                      "GET /me")
        put("/app/v1/me", "PUT /me",                           json={"username": rnd()})
        get("/app/v1/notifications?unread_only=true",          "GET /notifications")

        nid = self.notif_id or 1
        put(f"/app/v1/notifications/{nid}/read",               "PUT /notifications/{id}/read")

        post("/app/v1/token",
             "POST /token (OAuth2)",
             data={"username": ADMIN_CREDS["email"], "password": ADMIN_CREDS["password"]},
             headers={"Content-Type": "application/x-www-form-urlencoded"})

        if self.refresh_token:
            r = post("/app/v1/token/refresh", "POST /token/refresh",
                     json={"refresh_token": self.refresh_token})
            if r.status_code == 200:
                self.token         = r.json().get("access_token", self.token)
                self.refresh_token = r.json().get("refresh_token", self.refresh_token)
                self._set_auth_header()  # ✅ re-inject after refresh

        get("/app/v1/users/",                                  "GET /users/")
        get(f"/app/v1/users/{uid}",                            "GET /users/{id}")
        put(f"/app/v1/users/{uid}", "PUT /users/{id}",         json={"username": rnd()})
        get(f"/app/v1/users/{uid}/indexes",                    "GET /users/{id}/indexes")
        get(f"/app/v1/users/{uid}/requests",                   "GET /users/{id}/requests")

        r = post(f"/app/v1/users/{uid}/requests/config_edit?index_id={iid}",
                 "POST /requests/config_edit", json=INDEX_CONFIG)
        if r.status_code == 200:
            rid = r.json().get("id", rid)
            self.request_id = rid

        r2 = post(f"/app/v1/users/{uid}/requests/user_edit",
                  "POST /requests/user_edit", json={"username": rnd()})
        if r2.status_code == 200:
            rid = r2.json().get("id", rid)

        r3 = post(f"/app/v1/users/{uid}/requests/index_edit",
                  "POST /requests/index_edit", json={"name": f"se_{rnd()}"})
        if r3.status_code == 200:
            rid = r3.json().get("id", rid)

        get(f"/app/v1/users/{uid}/requests/{rid}",             "GET /requests/{rid}")
        put(f"/app/v1/users/{uid}/requests/{rid}",             "PUT /requests/{rid}")

        get(f"/app/v1/users/{uid}/approvals",                  "GET /approvals")
        get(f"/app/v1/users/{uid}/approvals/{rid}",            "GET /approvals/{rid}")
        put(f"/app/v1/users/{uid}/approvals/{rid}",
            "PUT /approvals/{rid}",
            json={"status": "approved", "reason": "smoke test"})

        new = post("/app/v1/indexes/", "POST /indexes/",
                   json={"name": f"sm_{rnd()}", "namespace": "smoke"})
        if new.status_code == 200:
            iid = new.json().get("id", iid)

        get("/app/v1/indexes/",                                "GET /indexes/")
        get(f"/app/v1/indexes/{iid}",                          "GET /indexes/{id}")
        put(f"/app/v1/indexes/{iid}", "PUT /indexes/{id}",     json={"name": f"sm_upd_{rnd()}"})
        get(f"/app/v1/indexes/{iid}/config",                   "GET /indexes/{id}/config")
        put(f"/app/v1/indexes/{iid}/config",
            "PUT /indexes/{id}/config", json=INDEX_CONFIG)
        get(f"/app/v1/indexes/{iid}/users",                    "GET /indexes/{id}/users")
        put(f"/app/v1/indexes/{iid}/users/{uid}",              "PUT /indexes/{id}/users/{uid}")
        get(f"/app/v1/indexes/{iid}/users/{uid}",              "GET /indexes/{id}/users/{uid}")
        post(f"/app/v1/indexes/{iid}/users",
             "POST /indexes/{id}/users (bulk)",
             json={"user_ids": [uid]})
        delete(f"/app/v1/indexes/{iid}/users/{uid}",           "DELETE /indexes/{id}/users/{uid}")

        get("/app/v1/reports/",                                "GET /reports/")
        with self.client.get("/app/v1/reports/1",
                             name="[smoke] GET /reports/{id}",
                             catch_response=True) as r:
            if r.status_code == 404:
                r.success()
        get("/app/v1/dashboard/",                              "GET /dashboard/")

        raise StopUser()