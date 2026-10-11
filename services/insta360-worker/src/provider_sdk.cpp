#include "openstoryline/provider.hpp"

#ifdef OPENSTORYLINE_MOCK_INSTA360_SDK
#include "mock_insta360_sdk.hpp"
#else
#include <ins_stitcher.h>
#endif

#include <chrono>
#include <algorithm>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "openstoryline/discovery.hpp"
#include "openstoryline/sha256.hpp"

namespace openstoryline::worker {
namespace {

std::once_flag g_sdk_initialization;

std::filesystem::path configured_model_root(
    const std::filesystem::path& requested) {
  if (!requested.empty()) {
    return requested;
  }
  if (const char* environment = std::getenv("INSTA360_MODEL_ROOT");
      environment != nullptr && *environment != '\0') {
    return environment;
  }
  return INSTA360_DEFAULT_MODEL_ROOT;
}

ins::STITCH_TYPE stitch_type(const std::string& value) {
  if (value == "template") {
    return ins::STITCH_TYPE::TEMPLATE;
  }
  if (value == "optical_flow") {
    return ins::STITCH_TYPE::OPTFLOW;
  }
  if (value == "dynamic") {
    return ins::STITCH_TYPE::DYNAMICSTITCH;
  }
  return ins::STITCH_TYPE::AIFLOW;
}

std::string sdk_error_code(int error) {
  switch (error) {
    case 1:
      return "sdk_open_file";
    case 2:
      return "sdk_parse_metadata";
    case 3:
      return "sdk_create_offscreen";
    case 4:
      return "sdk_create_render_model";
    case 5:
      return "sdk_frame_parse";
    case 6:
      return "sdk_create_render_source";
    case 7:
      return "sdk_render_frame";
    case 8:
      return "sdk_save_frame";
    case 9:
      return "sdk_video_exporter";
    default:
      return "sdk_failure";
  }
}

void remove_partial_output(const std::filesystem::path& path) {
  std::error_code error;
  std::filesystem::remove(path, error);
}

std::string sanitize_sdk_message(
    std::string message, const StitchRequest& request,
    const std::filesystem::path& staged_output) {
  std::vector<std::string> sensitive_paths;
  for (const auto& path : request.input_paths) {
    sensitive_paths.push_back(path.string());
    sensitive_paths.push_back(
        std::filesystem::absolute(path).lexically_normal().string());
  }
  sensitive_paths.push_back(request.output_path.string());
  sensitive_paths.push_back(staged_output.string());
  if (!request.model_root.empty()) {
    sensitive_paths.push_back(request.model_root.string());
  }
  for (const auto& sensitive : sensitive_paths) {
    if (sensitive.empty()) {
      continue;
    }
    std::size_t position = 0;
    while ((position = message.find(sensitive, position)) != std::string::npos) {
      message.replace(position, sensitive.size(), "<path>");
      position += 6;
    }
  }
  std::replace(message.begin(), message.end(), '\n', ' ');
  std::replace(message.begin(), message.end(), '\r', ' ');
  if (message.size() > 2000) {
    message.resize(2000);
  }
  return message;
}

}  // namespace

Insta360SdkStitchProvider::Insta360SdkStitchProvider() {
  std::call_once(g_sdk_initialization, [] {
    ins::SetLogLevel(ins::InsLogLevel::ERR);
    ins::InitEnv();
  });
}

CapabilityResponse Insta360SdkStitchProvider::capabilities() const {
  const auto model_root = configured_model_root({});
  CapabilityResponse response{
      OPENSTORYLINE_WORKER_VERSION,
      "insta360-mediasdk",
      true,
      INSTA360_SDK_VERSION,
      true,
      true,
      true,
      true,
      {"h264", "h265"},
      {"template", "optical_flow", "dynamic", "ai"},
      2,
      true,
      "Ubuntu 22.04 x86-64 with supported GPU/runtime",
      !model_root.empty() && std::filesystem::is_directory(model_root),
      {},
  };
#ifndef INSTA360_SDK_HAS_MODEL_ROOT_API
  response.supported_stitch_types = {"template", "optical_flow", "dynamic"};
  response.warnings.push_back(
      "This build lacks SetModelFileRootDir; AI stitching is disabled.");
#endif
#ifndef INSTA360_SDK_HAS_STITCH_FUSION
  response.warnings.push_back("This build lacks stitch-fusion support.");
#endif
  return response;
}

ProbeResponse Insta360SdkStitchProvider::probe(const ProbeRequest& request) const {
  ProbeResponse response;
  response.provider = "insta360-mediasdk";
  response.decodable = true;
  for (const auto& path : request.input_paths) {
    response.inputs.push_back(inspect_file(path));
  }
  response.warnings.push_back(
      "The current SDK adapter validates file access; detailed stream metadata is reported after stitch.");
  return response;
}

StitchResult Insta360SdkStitchProvider::stitch(
    const StitchRequest& request, const ProgressCallback& progress,
    const std::atomic_bool& cancelled) {
  for (const auto& path : request.input_paths) {
    static_cast<void>(inspect_file(path));
  }
  const auto output = std::filesystem::absolute(request.output_path).lexically_normal();
  if (std::filesystem::exists(output) && !request.overwrite) {
    throw WorkerException("output_exists", "output exists and overwrite is false",
                          ExitCode::kValidation, {{"output_path", output.string()}});
  }
  std::filesystem::create_directories(output.parent_path());
  const auto staged_output = output.parent_path() /
      ("." + output.stem().string() + "." + request.job_id + ".partial.mp4");
  remove_partial_output(staged_output);

  const auto model_root = configured_model_root(request.model_root);
  if (request.stitch_type == "ai") {
#ifdef INSTA360_SDK_HAS_MODEL_ROOT_API
    if (model_root.empty() || !std::filesystem::is_directory(model_root)) {
      throw WorkerException("model_root_required",
                            "AI stitching requires a valid model_root",
                            ExitCode::kCapability);
    }
    ins::SetModelFileRootDir(model_root.string());
#else
    throw WorkerException("ai_stitch_unavailable",
                          "AI stitching is unavailable in this SDK build",
                          ExitCode::kCapability);
#endif
  }
  if (request.enable_stitch_fusion) {
#ifndef INSTA360_SDK_HAS_STITCH_FUSION
    throw WorkerException("stitch_fusion_unavailable",
                          "stitch fusion was requested but is unsupported",
                          ExitCode::kCapability);
#endif
  }

  const auto log_path = output.string() + ".sdk.log";
  ins::SetLogPath(log_path);
  auto stitcher = std::make_shared<ins::VideoStitcher>();
  std::vector<std::string> input_paths;
  input_paths.reserve(request.input_paths.size());
  for (const auto& path : request.input_paths) {
    input_paths.push_back(std::filesystem::absolute(path).lexically_normal().string());
  }
  stitcher->SetInputPath(input_paths);
  stitcher->SetOutputPath(staged_output.string());
  stitcher->SetOutputSize(request.output_width, request.output_height);
  stitcher->SetOutputBitRate(request.bitrate_bps);
  stitcher->SetStitchType(stitch_type(request.stitch_type));
  stitcher->EnableFlowState(request.enable_flowstate);
  stitcher->EnableDirectionLock(request.enable_direction_lock);
#ifdef INSTA360_SDK_HAS_STITCH_FUSION
  stitcher->EnableStitchFusion(request.enable_stitch_fusion);
#endif
  if (request.codec == "h265") {
    stitcher->EnableH265Encoder();
  }

  std::mutex mutex;
  std::condition_variable condition;
  bool finished = false;
  int sdk_error = 0;
  std::string sdk_message;
  stitcher->SetStitchProgressCallback([&](int percent, int error) {
    static_cast<void>(error);
    progress(percent, "MediaSDK stitch progress");
    if (percent >= 100) {
      {
        std::lock_guard<std::mutex> lock(mutex);
        finished = true;
      }
      condition.notify_all();
    }
  });
  stitcher->SetStitchStateCallback([&](int error, const char* message) {
    {
      std::lock_guard<std::mutex> lock(mutex);
      sdk_error = error;
      sdk_message = message == nullptr ? "MediaSDK reported an error" : message;
    }
    condition.notify_all();
  });

  const auto started = std::chrono::steady_clock::now();
  progress(0, "starting MediaSDK stitch");
  stitcher->StartStitch();
  bool cancellation_requested = false;
  {
    std::unique_lock<std::mutex> lock(mutex);
    while (!finished && sdk_error == 0) {
      condition.wait_for(lock, std::chrono::milliseconds(200));
      if (cancelled.load() && !cancellation_requested) {
        cancellation_requested = true;
        lock.unlock();
        stitcher->CancelStitch();
        lock.lock();
      }
      if (cancellation_requested) {
        break;
      }
    }
  }
  if (cancellation_requested) {
    remove_partial_output(staged_output);
    throw WorkerException("cancelled", "MediaSDK stitch was cancelled",
                          ExitCode::kCancelled);
  }
  if (sdk_error != 0) {
    remove_partial_output(staged_output);
    throw WorkerException(sdk_error_code(sdk_error), "MediaSDK stitch failed",
                          ExitCode::kProvider,
                          {{"sdk_error", std::to_string(sdk_error)},
                           {"sdk_message", sanitize_sdk_message(
                                               sdk_message, request, staged_output)}});
  }
  if (!std::filesystem::is_regular_file(staged_output) ||
      std::filesystem::file_size(staged_output) == 0) {
    remove_partial_output(staged_output);
    throw WorkerException("output_validation_failed",
                          "MediaSDK completed without a nonempty output",
                          ExitCode::kOutputValidation);
  }
  const std::string output_hash = sha256_file(staged_output);
  std::error_code rename_error;
  std::filesystem::rename(staged_output, output, rename_error);
  if (rename_error) {
    remove_partial_output(staged_output);
    throw WorkerException("output_commit_failed",
                          "validated MediaSDK output could not be committed",
                          ExitCode::kOutputValidation,
                          {{"error", rename_error.message()}});
  }

  StitchResult result;
  result.job_id = request.job_id;
  result.provider = "insta360-mediasdk";
  result.input_paths = request.input_paths;
  for (const auto& path : request.input_paths) {
    result.source_sha256.push_back(sha256_file(path));
  }
  result.output_path = output;
  result.output_sha256 = output_hash;
  result.output_width = request.output_width;
  result.output_height = request.output_height;
  result.codec = request.codec;
  result.bitrate_bps = request.bitrate_bps;
  result.enable_flowstate = request.enable_flowstate;
  result.enable_direction_lock = request.enable_direction_lock;
  result.stitch_type = request.stitch_type;
  result.enable_stitch_fusion = request.enable_stitch_fusion;
  result.elapsed_seconds = std::chrono::duration<double>(
                               std::chrono::steady_clock::now() - started)
                               .count();
  if (std::filesystem::is_regular_file(log_path)) {
    result.sdk_log_path = log_path;
  }
  return result;
}

}  // namespace openstoryline::worker
