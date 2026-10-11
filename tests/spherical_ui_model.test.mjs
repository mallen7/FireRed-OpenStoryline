import assert from "node:assert/strict";
import test from "node:test";

import {
  CameraTrackEditorModel,
  sampleTrack,
} from "../web/static/spherical_workspace.js";

const asset = {
  asset_id: "asset-ui-model",
  duration_seconds: 10,
};

test("manual editor supports add, delete, undo, redo, easing, and subject", () => {
  const model = CameraTrackEditorModel.create(asset);
  assert.equal(model.track.created_by, "manual");
  assert.equal(model.track.keyframes.length, 2);

  model.addKeyframe(5, {
    yaw: 45,
    pitch: 12,
    roll: -3,
    horizontal_fov: 72,
    easing: "ease_in_out",
    subject_id: "subject-1",
    confidence: 0.91,
  });
  assert.equal(model.track.keyframes.length, 3);
  assert.equal(model.track.keyframes[1].subject_id, "subject-1");
  assert.equal(model.track.keyframes[1].easing, "ease_in_out");

  model.deleteKeyframe(1);
  assert.equal(model.track.keyframes.length, 2);
  assert.equal(model.undo(), true);
  assert.equal(model.track.keyframes.length, 3);
  assert.equal(model.redo(), true);
  assert.equal(model.track.keyframes.length, 2);
});

test("viewer interpolation takes the shortest path across the spherical seam", () => {
  const model = CameraTrackEditorModel.create({ ...asset, duration_seconds: 2 });
  model.updateKeyframe(0, { yaw: 170, easing: "linear" });
  model.updateKeyframe(1, { yaw: -170, easing: "linear" });
  const midpoint = sampleTrack(model.track, 1);
  assert.ok(Math.abs(Math.abs(midpoint.yaw) - 180) < 0.001, midpoint.yaw);
});

test("save revisions do not discard browser undo history", () => {
  const model = CameraTrackEditorModel.create(asset);
  model.updateTrack({ name: "Operator camera" });
  model.markSaved(4);
  assert.equal(model.revision, 4);
  assert.equal(model.dirty, false);
  assert.equal(model.undo(), true);
  assert.equal(model.track.name, "Manual camera");
  assert.equal(model.dirty, true);
});
