"""The deployed topology must match the code.

Process separation is only real if the manifests say so, and scheduled work is
only real if every command a CronJob names still exists. These tests read the
actual Fleet manifest and the container entrypoint, so a change to one without
the other fails the build rather than silently reverting the architecture.
"""

import ast
import pathlib
import re

import yaml
from django.test import SimpleTestCase

ROOT = pathlib.Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "fleet" / "bminfo" / "workloads.yaml"
ENTRYPOINT = ROOT / "bminfo" / "entrypoint.sh"
COMMANDS = ROOT / "bminfo" / "dashboard" / "management" / "commands"
COMPOSE_FILES = [ROOT / "docker-compose.yml", ROOT / "compose-dockge.yaml"]


def manifest_documents():
    return [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]


def find(kind, name):
    for doc in manifest_documents():
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    return None


class TopologyTests(SimpleTestCase):
    def test_collector_runs_in_its_own_deployment(self):
        collector = find("Deployment", "collector")
        self.assertIsNotNone(collector, "the collector Deployment is missing")

        container = collector["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["./entrypoint.sh", "collector"])
        # One collector only: a second would duplicate ingestion and connections.
        self.assertEqual(collector["spec"]["replicas"], 1)

    def test_web_deployment_does_not_start_the_collector(self):
        web = find("Deployment", "web")
        container = web["spec"]["template"]["spec"]["containers"][0]

        self.assertEqual(container["command"], ["./entrypoint.sh", "web"])
        env_names = set()
        for source in container.get("envFrom", []):
            env_names.add(source.get("secretRef", {}).get("name"))
        # The collector flag used to switch ingestion on inside the web pod; the
        # role now decides, so the deployment must not pass it.
        self.assertNotIn("APP_COLLECTOR_ENABLED", container.get("env", {}) or {})

    def test_recurring_work_is_scheduled_not_threaded(self):
        expected = {
            "archive-raw-events": "archive_raw_events",
            "cleanup-raw-events": "cleanup_raw_events",
            "sync-talkgroups": "sync_talkgroups",
            "purge-unverified-users": "purge_unverified_users",
        }
        for name, command in expected.items():
            with self.subTest(name=name):
                job = find("CronJob", name)
                self.assertIsNotNone(job, f"CronJob {name} is missing")
                args = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"]
                self.assertEqual(args[:1], ["python"])
                self.assertIn(command, args)

    def test_every_scheduled_command_exists(self):
        """A CronJob naming a command that no longer exists fails every night."""
        available = {path.stem for path in COMMANDS.glob("*.py")}
        for doc in manifest_documents():
            text = yaml.safe_dump(doc)
            for match in re.finditer(r"manage\.py['\"]?,\s*['\"](\w+)['\"]", text):
                with self.subTest(command=match.group(1)):
                    self.assertIn(match.group(1), available)

    def test_schedules_do_not_overlap(self):
        """Archive must finish before cleanup, which must finish before purge.

        They are all daily UTC jobs; running archive and cleanup in the same
        minute would have cleanup deleting rows the archive is still streaming.
        """
        times = {}
        for doc in manifest_documents():
            if doc.get("kind") != "CronJob":
                continue
            spec = doc["spec"]
            minute, hour = spec["schedule"].split()[:2]
            self.assertEqual(spec.get("timeZone"), "Etc/UTC", "schedules must be pinned to UTC")
            times[doc["metadata"]["name"]] = (int(hour), int(minute))

        self.assertLess(times["archive-raw-events"], times["cleanup-raw-events"])
        self.assertLess(times["cleanup-raw-events"], times["purge-unverified-users"])

    def test_jobs_forbid_concurrent_runs(self):
        for doc in manifest_documents():
            if doc.get("kind") != "CronJob":
                continue
            with self.subTest(cronjob=doc["metadata"]["name"]):
                self.assertEqual(doc["spec"]["concurrencyPolicy"], "Forbid")

    def test_archive_job_can_reach_object_storage(self):
        job = find("CronJob", "archive-raw-events")
        container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        sources = {e.get("secretRef", {}).get("name"): e.get("secretRef", {}).get("optional")
                   for e in container["envFrom"]}
        self.assertIn("bminfo-object-store", sources)
        # Optional so the schedule does not fail before the credentials exist.
        self.assertTrue(sources["bminfo-object-store"])


class EntrypointTests(SimpleTestCase):
    def setUp(self):
        self.script = ENTRYPOINT.read_text()

    def test_script_is_syntactically_valid(self):
        import subprocess

        result = subprocess.run(["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exposes_web_collector_and_task_roles(self):
        for role in ("web", "collector", "task"):
            with self.subTest(role=role):
                self.assertRegex(self.script, rf"(?m)^\s*{role}\)\s*$")

    def test_web_role_does_not_start_the_collector(self):
        web_block = self.script.split("web)", 1)[1].split(";;", 1)[0]
        self.assertNotIn("collect_brandmeister", web_block)

    def test_collector_role_runs_the_collector(self):
        collector_block = self.script.split("collector)", 1)[1].split(";;", 1)[0]
        self.assertIn("collect_brandmeister", collector_block)

    def test_both_roles_migrate_through_the_advisory_lock(self):
        # Starting order no longer serialises migrations, so the lock does.
        self.assertGreaterEqual(self.script.count("manage.py db_migrate"), 2)
        self.assertNotIn("manage.py migrate\n", self.script)


class CollectorProcessTests(SimpleTestCase):
    """The collector process must not own recurring work any more."""

    def setUp(self):
        self.source = (COMMANDS / "collect_brandmeister.py").read_text()

    def test_collector_does_not_schedule_maintenance(self):
        for forbidden in ("archive_loop", "nightly_cleanup", "talkgroup_loop"):
            with self.subTest(symbol=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_collector_still_owns_its_heartbeat(self):
        # /status/ reads this to tell "connected" apart from "process alive".
        self.assertIn("heartbeat_loop", self.source)

    def test_removed_schedulers_are_gone_from_the_package(self):
        archiver = (ROOT / "bminfo" / "dashboard" / "archiver.py").read_text()
        maintenance = (ROOT / "bminfo" / "dashboard" / "maintenance.py").read_text()
        self.assertNotIn("def archive_loop", archiver)
        self.assertNotIn("def nightly_cleanup", maintenance)


class ComposeTopologyTests(SimpleTestCase):
    """The Compose stack mirrors the split, so local runs match production."""

    def test_each_compose_file_has_a_collector_service(self):
        for path in COMPOSE_FILES:
            with self.subTest(file=path.name):
                services = yaml.safe_load(path.read_text())["services"]
                self.assertIn("collector", services)
                self.assertEqual(services["collector"]["command"],
                                 ["./entrypoint.sh", "collector"])
                self.assertNotIn("APP_COLLECTOR_ENABLED",
                                 services["web"].get("environment", {}))

    def test_web_service_uses_the_web_role(self):
        for path in COMPOSE_FILES:
            with self.subTest(file=path.name):
                services = yaml.safe_load(path.read_text())["services"]
                self.assertEqual(services["web"]["command"], ["./entrypoint.sh", "web"])


class ScheduledCommandTests(SimpleTestCase):
    def test_every_command_module_parses(self):
        for path in COMMANDS.glob("*.py"):
            with self.subTest(command=path.stem):
                ast.parse(path.read_text())

    def test_management_commands_are_discovered(self):
        from django.core.management import get_commands

        commands = get_commands()
        for name in ("db_migrate", "cleanup_raw_events", "sync_talkgroups",
                     "archive_raw_events", "collect_brandmeister"):
            with self.subTest(command=name):
                self.assertIn(name, commands)
