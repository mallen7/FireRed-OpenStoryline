from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from open_storyline.spherical.distributed_protocol import (
    AnalysisSettings,
    AnalyzeJobSubmission,
    DISTRIBUTED_SCHEMA_MODELS,
    ReframeJobSubmission,
    RetryPolicy,
    WorkerArtifact,
    WorkerJobError,
    WorkerJobOperation,
    WorkerJobState,
    StitchJobSubmission,
    export_distributed_json_schemas,
)
from open_storyline.spherical.fixtures import generate_fixture
from open_storyline.spherical.job_runner import (
    ExecutionCancelled,
    ExecutionFailure,
    ExecutionOutcome,
    SubprocessJobExecutor,
)
from open_storyline.spherical.job_store import DurableJobStore
from open_storyline.spherical.manifests import sha256_file
from open_storyline.spherical.remote_worker_client import HttpSphericalWorkerClient
from open_storyline.spherical.schemas import (
    ReframeRenderRequest,
    SphericalAsset,
    VirtualCameraTrack,
)
from open_storyline.spherical.worker_http import create_worker_app
from open_storyline.spherical.worker_protocol import WorkerStitchRequest
from open_storyline.spherical.worker_security import (
    DistributedWorkerSettings,
    WorkerPathPolicy,
    WorkerSecurityError,
    sanitize_sdk_log,
    sanitize_text,
)
from tests.test_spherical_worker import write_fake_worker


TOKEN = "phase6-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def settings_for(root: Path, *, max_request_bytes: int = 2 * 1024 * 1024):
    media = root / "media"
    output = root / "output"
    upload = media / "uploads"
    media.mkdir(parents=True)
    output.mkdir(parents=True)
    return DistributedWorkerSettings(
        state_dir=root / "state",
        media_roots=[media],
        output_roots=[output],
        upload_root=upload,
        bearer_token=TOKEN,
        worker_executable=root / "missing-public-worker",
        max_request_bytes=max_request_bytes,
        max_upload_bytes=1024 * 1024,
        max_concurrent_jobs=1,
        max_queued_jobs=10,
        event_poll_seconds=0.02,
        shutdown_grace_seconds=1.0,
    )


def dummy_reframe_submission(
    settings: DistributedWorkerSettings,
    *,
    name: str = "output.mp4",
) -> ReframeJobSubmission:
    master = settings.media_roots[0] / "master.mp4"
    if not master.exists():
        master.write_bytes(b"synthetic equirectangular contract input")
    digest = sha256_file(master)
    asset = SphericalAsset(
        asset_id="distributed-asset",
        source_paths=[master],
        source_hashes=[digest],
        master_path=master,
        width=640,
        height=320,
        fps=10.0,
        duration_seconds=2.0,
    )
    track = VirtualCameraTrack(
        track_id="distributed-track",
        asset_id=asset.asset_id,
        name="static",
        output_width=320,
        output_height=180,
        source_start_seconds=0.0,
        source_end_seconds=2.0,
        keyframes=[{"time_seconds": 0.0}],
    )
    output = settings.output_roots[0] / name
    request = ReframeRenderRequest(
        asset=asset,
        track=track,
        output_path=output,
        workspace_root=settings.output_roots[0],
        cache_dir=settings.output_roots[0] / "cache",
    )
    return ReframeJobSubmission(
        request=request,
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.02),
    )


class ControlledExecutor:
    def execute(
        self,
        job,
        request,
        *,
        emit_progress,
        cancel_requested,
        shutdown_requested,
    ):
        output = Path(request["request"]["output_path"])
        emit_progress(10, "accepted by controlled executor", {})
        if "slow" in output.name:
            while not shutdown_requested():
                if cancel_requested():
                    raise ExecutionCancelled("controlled operation cancelled cleanly")
                time.sleep(0.01)
        if "retry" in output.name and job.attempt_count == 1:
            raise ExecutionFailure(
                WorkerJobError(
                    code="transient_test_failure",
                    message="retryable test failure",
                    retryable=True,
                )
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(f"job={job.job_id}".encode("utf-8"))
        emit_progress(80, "artifact written", {})
        artifact = WorkerArtifact(
            name="reframed-video",
            path=output,
            sha256=sha256_file(output),
            size_bytes=output.stat().st_size,
            media_type="video/mp4",
        )
        return ExecutionOutcome(
            result={"operation": job.operation.value, "output_path": str(output)},
            artifacts=[artifact],
            metrics={
                "duration_seconds": 0.01,
                "processing_fps": 100.0,
                "output_size_bytes": output.stat().st_size,
                "cache_hit": False,
            },
        )


def wait_for_state(client: TestClient, job_id: str, terminal: bool = True):
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers=AUTH)
        response.raise_for_status()
        payload = response.json()
        state = WorkerJobState(payload["state"])
        if state.terminal == terminal:
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not reach the expected state")


class DistributedWorkerHttpTests(unittest.TestCase):
    def test_auth_idempotency_streaming_retry_metrics_and_artifact_download(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-http-") as name:
            root = Path(name)
            settings = settings_for(root)
            app = create_worker_app(settings, executor=ControlledExecutor())
            submission = dummy_reframe_submission(settings, name="retry-output.mp4")
            with TestClient(app) as client:
                self.assertEqual(client.get("/health/live").status_code, 200)
                self.assertEqual(client.get("/v1/capabilities").status_code, 401)
                self.assertEqual(
                    client.get("/v1/capabilities", headers=AUTH).status_code, 200
                )
                response = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-retry-job"},
                    json=submission.model_dump(mode="json"),
                )
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["job"]["job_id"]
                duplicate = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-retry-job"},
                    json=submission.model_dump(mode="json"),
                )
                self.assertEqual(duplicate.status_code, 200)
                self.assertEqual(duplicate.json()["job"]["job_id"], job_id)
                self.assertTrue(duplicate.json()["idempotent_replay"])

                terminal = wait_for_state(client, job_id)
                self.assertEqual(terminal["state"], "succeeded")
                self.assertEqual(terminal["attempt_count"], 2)
                with client.stream(
                    "GET", f"/v1/jobs/{job_id}/events", headers=AUTH
                ) as stream:
                    body = "".join(stream.iter_text())
                self.assertIn("event: retrying", body)
                self.assertIn("event: succeeded", body)
                artifact = client.get(
                    f"/v1/jobs/{job_id}/artifacts/reframed-video", headers=AUTH
                )
                self.assertEqual(artifact.status_code, 200)
                self.assertEqual(artifact.content, f"job={job_id}".encode("utf-8"))
                metrics = client.get("/v1/metrics", headers=AUTH).json()
                self.assertEqual(metrics["job_counts"]["succeeded"], 1)
                self.assertEqual(metrics["operation_counts"]["reframe"], 1)

    def test_cancel_invalid_paths_upload_limits_and_idempotency_conflict(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-security-") as name:
            root = Path(name)
            settings = settings_for(root, max_request_bytes=4096)
            app = create_worker_app(settings, executor=ControlledExecutor())
            slow = dummy_reframe_submission(settings, name="slow-output.mp4")
            with TestClient(app) as client:
                uploaded = client.post(
                    "/v1/uploads/card/camera.insv",
                    headers={**AUTH, "Content-Type": "application/octet-stream"},
                    content=b"raw-camera-bytes",
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                self.assertTrue(Path(uploaded.json()["path"]).is_file())
                traversal = client.post(
                    "/v1/uploads/../escape.insv", headers=AUTH, content=b"x"
                )
                self.assertIn(traversal.status_code, {404, 422})

                response = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-cancel-job"},
                    json=slow.model_dump(mode="json"),
                )
                job_id = response.json()["job"]["job_id"]
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    current = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
                    if current["state"] == "running":
                        break
                    time.sleep(0.01)
                cancelled = client.post(
                    f"/v1/jobs/{job_id}/cancel", headers=AUTH
                )
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(wait_for_state(client, job_id)["state"], "cancelled")

                conflict_submission = dummy_reframe_submission(
                    settings, name="different-output.mp4"
                )
                conflict = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-cancel-job"},
                    json=conflict_submission.model_dump(mode="json"),
                )
                self.assertEqual(conflict.status_code, 409)

                escaped = slow.model_dump(mode="json")
                escaped["request"]["output_path"] = str(root / "escape.mp4")
                escaped["request"]["workspace_root"] = str(root)
                invalid = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-invalid-path"},
                    json=escaped,
                )
                self.assertEqual(invalid.status_code, 422)

                oversized = client.post(
                    "/v1/jobs/reframe",
                    headers={**AUTH, "Idempotency-Key": "phase6-oversized"},
                    content=b"{" + b"x" * 5000 + b"}",
                )
                self.assertEqual(oversized.status_code, 413)

    def test_remote_client_submits_and_streams_over_http_transport(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-client-") as name:
            root = Path(name)
            settings = settings_for(root)
            app = create_worker_app(settings, executor=ControlledExecutor())
            submission = dummy_reframe_submission(settings)
            with TestClient(app) as api:
                client = HttpSphericalWorkerClient(
                    "http://testserver",
                    TOKEN,
                    transport=api._transport,
                )
                try:
                    self.assertIn(
                        WorkerJobOperation.REFRAME,
                        client.service_capabilities().operations,
                    )
                    accepted = client.submit_reframe(
                        submission, idempotency_key="phase6-remote-client"
                    )
                    events = []
                    job = client.wait_for_job(
                        str(accepted.job.job_id), on_event=events.append
                    )
                    self.assertEqual(job.state, WorkerJobState.SUCCEEDED)
                    self.assertTrue(any(event.progress_percent == 80 for event in events))
                    destination = root / "downloaded.mp4"
                    client.download_artifact(
                        str(job.job_id),
                        "reframed-video",
                        destination,
                        expected_sha256=job.artifacts[0].sha256,
                    )
                    self.assertEqual(sha256_file(destination), job.artifacts[0].sha256)
                finally:
                    client.close()


class DistributedWorkerDurabilityTests(unittest.TestCase):
    def test_distributed_schemas_match_checked_in_contracts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-schemas-") as name:
            generated = export_distributed_json_schemas(name)
            self.assertEqual(len(generated), len(DISTRIBUTED_SCHEMA_MODELS))
            checked_root = Path("services/insta360-worker/schemas")
            for path in generated:
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    json.loads((checked_root / path.name).read_text(encoding="utf-8")),
                )

    def test_restart_recovers_running_jobs_and_preserves_completed_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-store-") as name:
            root = Path(name)
            store = DurableJobStore(root / "jobs.sqlite3", max_queued_jobs=10)
            request = {"schema_version": "1.0", "request": {"value": 1}}
            job, created = store.create_or_get(
                operation=WorkerJobOperation.REFRAME,
                idempotency_key="phase6-durable-job",
                request=request,
                timeout_seconds=30,
                retry_policy=RetryPolicy(max_attempts=2),
            )
            self.assertTrue(created)
            claimed = store.claim_next()
            self.assertEqual(claimed.job_id, job.job_id)
            restarted = DurableJobStore(root / "jobs.sqlite3", max_queued_jobs=10)
            self.assertEqual(restarted.recover_interrupted(), 1)
            self.assertEqual(restarted.get(job.job_id).state, WorkerJobState.QUEUED)
            claimed_again = restarted.claim_next()
            artifact_path = root / "artifact.mp4"
            artifact_path.write_bytes(b"persisted")
            artifact = WorkerArtifact(
                name="output",
                path=artifact_path,
                sha256=sha256_file(artifact_path),
                size_bytes=artifact_path.stat().st_size,
            )
            restarted.complete(
                claimed_again.job_id,
                result={"ok": True},
                artifacts=[artifact],
                metrics={"duration_seconds": 1.0},
            )
            after_second_restart = DurableJobStore(
                root / "jobs.sqlite3", max_queued_jobs=10
            ).get(job.job_id)
            self.assertEqual(after_second_restart.state, WorkerJobState.SUCCEEDED)
            self.assertEqual(after_second_restart.result, {"ok": True})
            self.assertEqual(after_second_restart.artifacts[0].sha256, artifact.sha256)

    def test_path_policy_rejects_symlink_escape_and_client_model_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-policy-") as name:
            root = Path(name)
            settings = settings_for(root)
            outside = root / "outside.mp4"
            outside.write_bytes(b"outside")
            link = settings.media_roots[0] / "linked.mp4"
            link.symlink_to(outside)
            policy = WorkerPathPolicy(settings)
            with self.assertRaises(WorkerSecurityError):
                policy.input_file(link)
            raw = settings.media_roots[0] / "camera.insv"
            raw.write_bytes(b"raw")
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=settings.output_roots[0] / "master.mp4",
                output_width=640,
                output_height=320,
                model_root=root / "client-selected-models",
            )
            with self.assertRaisesRegex(WorkerSecurityError, "may not select"):
                policy.normalize_stitch(StitchJobSubmission(request=request))

    def test_bearer_and_sdk_log_secrets_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-redaction-") as name:
            path = Path(name) / "sdk.log"
            path.write_text(
                "Authorization: Bearer top.secret-token\napi_key=visible-secret\n",
                encoding="utf-8",
            )
            sanitize_sdk_log(path)
            sanitized = path.read_text(encoding="utf-8")
            self.assertNotIn("top.secret-token", sanitized)
            self.assertNotIn("visible-secret", sanitized)
            self.assertIn("[REDACTED]", sanitized)
            self.assertNotIn(
                "token-value",
                sanitize_text("Bearer token-value password=another-value"),
            )


class DistributedWorkerSubprocessTests(unittest.TestCase):
    def test_remote_http_executes_existing_reframe_and_analysis_pipelines(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-http-real-") as name:
            root = Path(name)
            settings = settings_for(root)
            fixture = generate_fixture(
                settings.media_roots[0] / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            asset = SphericalAsset.model_validate_json(
                fixture.asset.read_text(encoding="utf-8")
            )
            track = VirtualCameraTrack.model_validate_json(
                fixture.static_track.read_text(encoding="utf-8")
            )
            reframe = ReframeJobSubmission(
                request=ReframeRenderRequest(
                    asset=asset,
                    track=track,
                    output_path=settings.output_roots[0] / "remote-real.mp4",
                    workspace_root=settings.output_roots[0],
                    cache_dir=settings.output_roots[0] / "render-cache",
                ),
                retry_policy=RetryPolicy(max_attempts=1),
            )
            analyze = AnalyzeJobSubmission(
                asset=asset,
                cache_dir=settings.output_roots[0] / "analysis-cache",
                settings=AnalysisSettings(
                    interval_seconds=1.0,
                    max_frames=1,
                    yaw_step_degrees=90,
                    pitch_centers=[-45.0, 0.0, 45.0],
                    horizontal_fov=100.0,
                    view_width=96,
                    view_height=96,
                ),
                retry_policy=RetryPolicy(max_attempts=1),
            )
            app = create_worker_app(settings)
            with TestClient(app) as api:
                remote = HttpSphericalWorkerClient(
                    "http://testserver", TOKEN, transport=api._transport
                )
                try:
                    manifest = remote.reframe(
                        reframe, idempotency_key="phase6-http-real-reframe"
                    )
                    analysis = remote.analyze(
                        analyze, idempotency_key="phase6-http-real-analysis"
                    )
                finally:
                    remote.close()
            self.assertTrue(manifest.output_path.is_file())
            self.assertEqual(analysis.asset_id, asset.asset_id)
            self.assertEqual(len(analysis.sample_timestamps), 1)

    def test_remote_client_submits_real_isolated_stitch_and_streams_progress(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-stitch-") as name:
            root = Path(name)
            settings = settings_for(root)
            fixture = generate_fixture(
                settings.media_roots[0] / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            settings.worker_executable = write_fake_worker(root, fixture.video)
            raw = settings.media_roots[0] / "camera.insv"
            raw.write_bytes(b"shared raw input")
            output = settings.output_roots[0] / "stitched.mp4"
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=output,
                output_width=640,
                output_height=320,
                codec="h264",
                bitrate_bps=10_000_000,
                stitch_type="template",
                enable_stitch_fusion=False,
            )
            app = create_worker_app(settings)
            with TestClient(app) as api:
                remote = HttpSphericalWorkerClient(
                    "http://testserver", TOKEN, transport=api._transport
                )
                progress = []
                try:
                    result = remote.stitch(
                        request,
                        on_progress=lambda event: progress.append(
                            event.progress_percent
                        ),
                    )
                finally:
                    remote.close()
            self.assertEqual(result.output_sha256, sha256_file(output))
            self.assertIn(0, progress)
            self.assertIn(100, progress)

    def test_http_cancellation_terminates_isolated_stitch_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-stitch-cancel-") as name:
            root = Path(name)
            settings = settings_for(root)
            master = root / "unused.mp4"
            master.write_bytes(b"unused")
            settings.worker_executable = write_fake_worker(
                root, master, behavior="wait"
            )
            raw = settings.media_roots[0] / "camera.insv"
            raw.write_bytes(b"shared raw input")
            output = settings.output_roots[0] / "cancelled.mp4"
            request = WorkerStitchRequest(
                job_id=uuid4(),
                input_paths=[raw],
                output_path=output,
                output_width=640,
                output_height=320,
                stitch_type="template",
            )
            app = create_worker_app(settings)
            with TestClient(app) as api:
                submitted = api.post(
                    "/v1/jobs/stitch",
                    headers={**AUTH, "Idempotency-Key": "phase6-real-cancel"},
                    json={
                        "schema_version": "1.0",
                        "request": request.model_dump(mode="json"),
                        "retry_policy": {
                            "schema_version": "1.0",
                            "max_attempts": 1,
                            "initial_delay_seconds": 0,
                            "maximum_delay_seconds": 0,
                        },
                    },
                )
                self.assertEqual(submitted.status_code, 202, submitted.text)
                job_id = submitted.json()["job"]["job_id"]
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    current = api.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
                    if current["state"] == "running":
                        break
                    time.sleep(0.02)
                time.sleep(0.15)
                api.post(f"/v1/jobs/{job_id}/cancel", headers=AUTH).raise_for_status()
                terminal = wait_for_state(api, job_id)
                self.assertEqual(terminal["state"], "cancelled")
                self.assertFalse(output.exists())

                timed_output = settings.output_roots[0] / "timed-out.mp4"
                timed_request = request.model_copy(
                    update={"job_id": uuid4(), "output_path": timed_output}
                )
                timed = api.post(
                    "/v1/jobs/stitch",
                    headers={**AUTH, "Idempotency-Key": "phase6-real-timeout"},
                    json={
                        "schema_version": "1.0",
                        "request": timed_request.model_dump(mode="json"),
                        "timeout_seconds": 0.05,
                        "retry_policy": {
                            "schema_version": "1.0",
                            "max_attempts": 1,
                            "initial_delay_seconds": 0,
                            "maximum_delay_seconds": 0,
                        },
                    },
                )
                timed_job_id = timed.json()["job"]["job_id"]
                timed_terminal = wait_for_state(api, timed_job_id)
                self.assertEqual(timed_terminal["state"], "timed_out")
                self.assertFalse(timed_output.exists())

    def test_isolated_subprocess_executes_existing_real_reframe_pipeline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="distributed-real-") as name:
            root = Path(name)
            settings = settings_for(root)
            fixture = generate_fixture(
                settings.media_roots[0] / "fixture",
                width=640,
                height=320,
                fps=10.0,
                duration_seconds=1.0,
                output_width=320,
                output_height=180,
            )
            asset = SphericalAsset.model_validate_json(
                fixture.asset.read_text(encoding="utf-8")
            )
            track = VirtualCameraTrack.model_validate_json(
                fixture.static_track.read_text(encoding="utf-8")
            )
            submission = ReframeJobSubmission(
                request=ReframeRenderRequest(
                    asset=asset,
                    track=track,
                    output_path=settings.output_roots[0] / "real-reframe.mp4",
                    workspace_root=settings.output_roots[0],
                    cache_dir=settings.output_roots[0] / "cache",
                ),
                retry_policy=RetryPolicy(max_attempts=1),
            )
            policy = WorkerPathPolicy(settings)
            normalized = policy.normalize_reframe(submission)
            store = DurableJobStore(settings.state_dir / "jobs.sqlite3", max_queued_jobs=10)
            job, _ = store.create_or_get(
                operation=WorkerJobOperation.REFRAME,
                idempotency_key="phase6-real-reframe",
                request=normalized.model_dump(mode="json"),
                timeout_seconds=60,
                retry_policy=normalized.retry_policy,
            )
            claimed = store.claim_next()
            progress = []
            outcome = SubprocessJobExecutor(settings, policy).execute(
                claimed,
                store.request_payload(job.job_id),
                emit_progress=lambda value, message, data: progress.append(value),
                cancel_requested=lambda: False,
                shutdown_requested=lambda: False,
            )
            self.assertTrue(Path(outcome.result["output_path"]).is_file())
            self.assertEqual(outcome.artifacts[0].name, "reframed-video")
            self.assertIn(100, progress)


if __name__ == "__main__":
    unittest.main()
