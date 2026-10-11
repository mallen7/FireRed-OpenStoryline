#include "openstoryline/provider.hpp"

#include "openstoryline/discovery.hpp"

namespace openstoryline::worker {

CapabilityResponse StubStitchProvider::capabilities() const {
  return CapabilityResponse{
      OPENSTORYLINE_WORKER_VERSION,
      "stub",
      false,
      {},
      false,
      false,
      true,
      true,
      {},
      {},
      2,
      false,
      "portable protocol-test build",
      false,
      {"Stub provider validates protocol and orchestration only; it cannot decode .insv."},
  };
}

ProbeResponse StubStitchProvider::probe(const ProbeRequest& request) const {
  ProbeResponse response;
  response.provider = "stub";
  response.decodable = false;
  for (const auto& path : request.input_paths) {
    response.inputs.push_back(inspect_file(path));
  }
  response.warnings.push_back(
      "Stub provider cannot decode .insv or inspect proprietary stream metadata.");
  return response;
}

StitchResult StubStitchProvider::stitch(const StitchRequest& request,
                                        const ProgressCallback& progress,
                                        const std::atomic_bool& cancelled) {
  if (cancelled.load()) {
    throw WorkerException("cancelled", "stitch job was cancelled before start",
                          ExitCode::kCancelled);
  }
  progress(0, "request validated; checking provider capability");
  throw WorkerException(
      "sdk_unavailable",
      "Stub provider does not decode .insv; build with an approved Insta360 MediaSDK.",
      ExitCode::kCapability,
      {{"provider", "stub"}, {"job_id", request.job_id}});
}

std::unique_ptr<StitchProvider> make_provider() {
#ifdef INSTA360_ENABLE_SDK
  return std::make_unique<Insta360SdkStitchProvider>();
#else
  return std::make_unique<StubStitchProvider>();
#endif
}

}  // namespace openstoryline::worker
