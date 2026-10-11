#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace openstoryline::worker {

inline constexpr const char* kSchemaVersion = "1.0";

enum class ExitCode : int {
  kSuccess = 0,
  kUsage = 2,
  kValidation = 3,
  kCapability = 4,
  kProvider = 5,
  kCancelled = 6,
  kOutputValidation = 7,
  kProtocol = 8,
};

struct WorkerException : public std::runtime_error {
  WorkerException(std::string stable_code, std::string message, ExitCode status,
                  std::map<std::string, std::string> context = {});
  std::string code;
  ExitCode exit_code;
  std::map<std::string, std::string> details;
};

struct InputMetadata {
  std::filesystem::path path;
  std::uintmax_t size_bytes{};
  double modified_unix_seconds{};
  bool readable{};
  std::string extension;
  std::map<std::string, std::string> stream_metadata;
};

struct ProbeRequest {
  std::vector<std::filesystem::path> input_paths;
};

struct ProbeResponse {
  std::string provider;
  bool decodable{};
  std::vector<InputMetadata> inputs;
  std::string camera_model;
  std::vector<std::string> warnings;
};

struct StitchRequest {
  std::string job_id;
  std::vector<std::filesystem::path> input_paths;
  std::filesystem::path output_path;
  int output_width{7680};
  int output_height{3840};
  std::string codec{"h265"};
  std::int64_t bitrate_bps{120000000};
  bool enable_flowstate{true};
  bool enable_direction_lock{false};
  std::string stitch_type{"ai"};
  bool enable_stitch_fusion{true};
  std::filesystem::path model_root;
  bool overwrite{false};
};

struct StitchResult {
  std::string job_id;
  std::string provider;
  std::vector<std::filesystem::path> input_paths;
  std::vector<std::string> source_sha256;
  std::filesystem::path output_path;
  std::string output_sha256;
  int output_width{};
  int output_height{};
  std::string codec;
  std::int64_t bitrate_bps{};
  bool enable_flowstate{};
  bool enable_direction_lock{};
  std::string stitch_type;
  bool enable_stitch_fusion{};
  double elapsed_seconds{};
  std::filesystem::path sdk_log_path;
  std::vector<std::string> warnings;
};

struct CapabilityResponse {
  std::string worker_version;
  std::string provider;
  bool sdk_enabled{};
  std::string sdk_version;
  bool can_decode_insv{};
  bool can_stitch{};
  bool supports_progress{};
  bool supports_cancellation{};
  std::vector<std::string> supported_codecs;
  std::vector<std::string> supported_stitch_types;
  int max_input_files{2};
  bool requires_gpu{};
  std::string target_platform;
  bool model_root_configured{};
  std::vector<std::string> warnings;
};

struct DiscoveryFile {
  InputMetadata metadata;
  std::string filename;
  std::string lens_marker;
};

struct DiscoveryGroup {
  std::string group_id;
  std::vector<DiscoveryFile> files;
  std::vector<std::filesystem::path> proposed_order;
  double confidence{};
  std::vector<std::string> reasons;
  std::vector<std::string> warnings;
  bool requires_confirmation{true};
};

struct DiscoveryResponse {
  std::filesystem::path directory;
  std::vector<DiscoveryGroup> groups;
  std::vector<DiscoveryFile> ungrouped;
  std::vector<std::string> warnings;
};

StitchRequest parse_stitch_request(const std::filesystem::path& path);
std::string capability_json(const CapabilityResponse& response);
std::string probe_json(const ProbeResponse& response);
std::string discovery_json(const DiscoveryResponse& response);
std::string progress_json(const std::string& job_id, int progress,
                          const std::string& message);
std::string result_json(const StitchResult& result);
std::string error_json(const WorkerException& error,
                       const std::string& job_id = {});
std::string now_utc_iso8601();

}  // namespace openstoryline::worker
