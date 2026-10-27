"""Deployment mode selection, command safety and shared data configuration."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import start


class StartupTests(unittest.TestCase):
    def test_config_accepts_both_modes_and_rejects_typo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.ini"
            for mode in ["normal", "lowmem"]:
                path.write_text(f"[runtime]\nmode={mode}\nsandbox=false\nproject=osa-lowmem\n")
                self.assertEqual(start.load_config(path), start.Startup(mode, False, "osa-lowmem"))
            for value in ["[runtime]\nmode=fast\nsandbox=false\nproject=osa-lowmem\n",
                          "[runtime]\nmode=normal\nsandbox=maybe\nproject=osa-lowmem\n",
                          "[runtime]\nmode=normal\nsandbox=false\nproject=Upper case\n",
                          "[runtime]\nmode=normal\nsandbox=false\nproject=osa\nmod=lowmem\n"]:
                path.write_text(value)
                with self.assertRaises(ValueError):
                    start.load_config(path)

    def test_host_environment_cannot_override_mode_or_enable_extra_profiles(self):
        base = {"OSA_PROFILE_WORKERS": "999", "OSA_PROFILE_HEAVY_SLOT": "/unwanted",
                "COMPOSE_PROFILES": "sandbox", "HTTPS_PROXY": "http://configured-proxy"}
        result = start.environment(start.Startup("normal", False, "osa"), base)
        self.assertEqual(result["OSA_PROFILE_WORKERS"], "10")
        self.assertEqual(result["OSA_PROFILE_HEAVY_SLOT"], "")
        self.assertEqual(result["OSA_PROFILE_SANDBOX"], "false")
        self.assertNotIn("COMPOSE_PROFILES", result)
        self.assertEqual(result["HTTPS_PROXY"], base["HTTPS_PROXY"])

    def test_lowmem_checks_before_any_build_or_container_change(self):
        commands = start.plan("up", start.Startup("lowmem", False, "osa"))
        self.assertEqual(commands[0][-2:], ["config", "--quiet"])
        self.assertIn("check_lowmem.py", commands[1][-1])
        self.assertEqual(commands[2][-1], "build")
        self.assertTrue(any(command[-2:]==["stop", "sandbox-worker"] for command in commands[3:]))
        self.assertEqual(commands[-1][-3:], ["up", "-d", "--remove-orphans"])

    def test_normal_and_no_build_choices_preserve_project_and_volumes(self):
        normal = start.Startup("normal", True, "osa-lowmem")
        low = start.Startup("lowmem", True, "osa-lowmem")
        self.assertEqual(start.compose(normal), start.compose(low))
        commands = start.plan("up", normal, no_build=True)
        self.assertFalse(any("build" in command for command in commands))
        self.assertFalse(any("check_lowmem.py" in " ".join(command) for command in commands))
        self.assertFalse(any("-v" in command or "--volumes" in command for command in start.plan("down", low)))
        self.assertIn("sandbox", start.plan("down", start.Startup("normal", False, "osa"))[0])

    def test_sandbox_builds_runner_and_enables_both_app_and_compose_profile(self):
        config = start.Startup("normal", True, "osa")
        self.assertEqual(start.environment(config, {})["OSA_PROFILE_SANDBOX"], "true")
        commands = start.plan("build", config)
        self.assertIn("sandbox", commands[1])
        self.assertEqual(commands[-1][:2], ["docker", "build"])
        self.assertIn("osa-python-runner:0.2", commands[-1])

    def test_failed_check_stops_before_build_and_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").touch()
            (root / "startup.ini").write_text("[runtime]\nmode=lowmem\nsandbox=false\nproject=osa\n")
            with patch.object(start, "ROOT", root), patch.object(start.subprocess, "run",
                    side_effect=subprocess.CalledProcessError(1, ["docker", "compose"])) as run:
                self.assertEqual(start.main(["up", "--config", str(root / "startup.ini")]), 1)
                run.assert_called_once()


@unittest.skipUnless(shutil.which("docker"), "Docker CLI is needed for static Compose checks")
class RenderedModeTests(unittest.TestCase):
    def rendered(self, mode, sandbox=False):
        # A disposable project containing no real credentials; this contacts no daemon.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "docker-compose.runtime.yml", root / "docker-compose.runtime.yml")
            (root / ".env").write_text("OSA_WORKER_TOKEN=static-test-worker-token-123456789\nOSA_RETENTION_DAYS=21\n")
            config = start.Startup(mode, sandbox, "shared-test")
            result = subprocess.run(start.compose(config, root) + ["config", "--format", "json"],
                env=start.environment(config), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_switch_preserves_database_login_volumes_and_retention(self):
        normal, low = self.rendered("normal"), self.rendered("lowmem")
        for service in ["app", "browser-worker"]:
            self.assertEqual(normal["services"][service]["volumes"], low["services"][service]["volumes"])
        for config in [normal, low]:
            app = config["services"]["app"]["environment"]
            self.assertEqual(app["OSA_DATABASE_URL"], "sqlite+aiosqlite:////app/data/osa.db")
            self.assertEqual(app["OSA_RETENTION_DAYS"], "21")
            self.assertEqual(app["OSA_DEEPSEEK_TRIGGER"], "always")
            self.assertEqual(app["OSA_SANDBOX_ENABLED"], "false")
        self.assertEqual(normal["volumes"], low["volumes"])
        self.assertEqual(normal["name"], low["name"])

    def test_lowmem_restricts_resources_and_serializes_all_heavy_workers(self):
        services = self.rendered("lowmem", True)["services"]
        self.assertEqual(services["app"]["environment"]["OSA_TASK_CONCURRENCY"], "1")
        self.assertEqual(services["browser-worker"]["environment"]["BROWSER_LAZY"], "true")
        self.assertEqual(services["app"]["environment"]["OSA_SANDBOX_ENABLED"], "true")
        self.assertEqual(int(services["app"]["mem_limit"]), 192 * 1024**2)
        for name in ["browser-worker", "indexer-worker", "screenshot-worker", "sandbox-worker"]:
            service = services[name]
            self.assertEqual(service["environment"]["OSA_HEAVY_SLOT"], "/slots/heavy.lock")
            self.assertGreater(int(service["memswap_limit"]), int(service["mem_limit"]))
            self.assertFalse(service.get("ports"))
        self.assertIn("/tmp:size=48m,noexec,nosuid", services["indexer-worker"]["tmpfs"])

    def test_normal_mode_raises_capacity_and_leaves_heavy_workers_independent(self):
        services = self.rendered("normal", True)["services"]
        self.assertEqual(services["app"]["environment"]["OSA_RUNTIME_MODE"], "normal")
        self.assertEqual(services["app"]["environment"]["OSA_WORKER_CONCURRENCY"], "4")
        self.assertEqual(services["app"]["environment"]["OSA_TASK_CONCURRENCY"], "3")
        self.assertEqual(services["browser-worker"]["environment"]["BROWSER_LAZY"], "false")
        self.assertEqual(int(services["browser-worker"]["mem_limit"]), 2 * 1024**3)
        for name in ["browser-worker", "indexer-worker", "screenshot-worker", "sandbox-worker"]:
            self.assertEqual(services[name]["environment"]["OSA_HEAVY_SLOT"], "")

class V2StartupTests(unittest.TestCase):
    def test_legacy_ini_keeps_sqlite_and_new_ini_validates_work_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'startup.ini'
            path.write_text('[runtime]\nmode=normal\nsandbox=false\nproject=osa\n')
            self.assertEqual(start.load_config(path).storage,'sqlite')
            self.assertFalse(start.load_config(path).work)
            path.write_text('[runtime]\nmode=normal\nsandbox=false\nproject=osa\nstorage=postgres\nwork=true\n')
            with self.assertRaises(ValueError):start.load_config(path)

    @unittest.skipUnless(shutil.which('docker'),'Docker CLI needed for static render')
    def test_postgres_modes_share_storage_and_work_has_no_gateway_secrets(self):
        outputs=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copyfile(ROOT/'docker-compose.runtime.yml',root/'docker-compose.runtime.yml')
            (root/'.env').write_text('OSA_WORKER_TOKEN=static-test-worker-token-123456789\nOSA_DB_PASSWORD=static-password-for-config-test-0123456789\n')
            for mode in ['normal','lowmem']:
                config=start.Startup(mode,True,'same-project','postgres',True)
                env=start.environment(config,{"PATH":os.environ.get("PATH","")},root)
                result=subprocess.run(start.compose(config,root)+['config','--format','json'],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                value=json.loads(result.stdout);outputs.append(value)
                services=value['services']
                self.assertIn('postgres',services);self.assertIn('work-worker',services)
                work=services['work-worker']
                self.assertNotIn('OSA_ADMIN_TOKEN',work['environment'])
                self.assertNotIn('OSA_NAPCAT_TOKEN',work['environment'])
                self.assertNotIn('/app/data',[v['target'] for v in work['volumes']])
                self.assertFalse(work.get('ports'))
                self.assertIn('postgresql',services['app']['environment']['OSA_DATABASE_URL'])
            self.assertEqual(outputs[0]['services']['app']['environment']['OSA_DATABASE_URL'],outputs[1]['services']['app']['environment']['OSA_DATABASE_URL'])
            self.assertEqual(outputs[0]['volumes'],outputs[1]['volumes'])

class BackupPlanTests(unittest.TestCase):
    def test_backup_freezes_writers_and_preserves_volumes(self):
        plan=start.plan('backup',start.Startup('normal',True,'osa','postgres',True))
        self.assertEqual(plan[1][-4:],['stop','app','work-worker','browser-worker'])
        self.assertTrue(any('/app/scripts/backup.py' in cmd for cmd in plan))
        self.assertTrue(any('backup-browser' in cmd for cmd in plan))
        self.assertFalse(any('-v' in cmd or '--volumes' in cmd for cmd in plan))
        self.assertEqual(plan[-1][-2:],['up','-d'])


class V3StartupTests(unittest.TestCase):
    def test_opencode_profile_build_and_volume_backup_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'startup.ini'
            path.write_text('[runtime]\nmode=normal\nsandbox=true\nproject=osa\nwork=true\ncoding=opencode\n')
            config = start.load_config(path)
            self.assertEqual(config.coding, 'opencode')
            self.assertIn('opencode', start.compose(config))
            self.assertEqual(start.environment(config, {})['OSA_PROFILE_CODING'], 'opencode')
            self.assertTrue(any('osa-opencode-runner:1.2.27' in cmd for cmd in start.plan('build', config)))
            self.assertTrue(any('--backup' in cmd for cmd in start.plan('backup', config)))

    @unittest.skipUnless(shutil.which('docker'), 'Docker CLI needed for static render')
    def test_opencode_gateway_has_only_dedicated_model_and_no_qq_database_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT/'docker-compose.runtime.yml', root/'docker-compose.runtime.yml')
            (root/'.env').write_text('OSA_WORKER_TOKEN=static-test-token-12345678901234567890\n')
            config = start.Startup('normal', True, 'v3-test', 'sqlite', True, 'opencode')
            rendered = subprocess.run(start.compose(config, root)+['config','--format','json'],
                env=start.environment(config, {'PATH': os.environ.get('PATH', '')}), capture_output=True, text=True, timeout=15)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            services = json.loads(rendered.stdout)['services']
            gateway = services['opencode-gateway']
            self.assertEqual(services['work-worker']['environment']['OSA_OPENCODE_ENDPOINT'], 'http://opencode-gateway:8094/projects/{workspace_id}')
            self.assertFalse(gateway.get('ports'))
            self.assertNotIn('OSA_ADMIN_TOKEN', gateway['environment'])
            self.assertNotIn('OSA_NAPCAT_TOKEN', gateway['environment'])
            self.assertEqual(gateway['environment']['OSA_OPENCODE_NETWORK'], 'v3-test_opencode_net')
            self.assertEqual([v['target'] for v in gateway['volumes']], ['/var/run/docker.sock'])
