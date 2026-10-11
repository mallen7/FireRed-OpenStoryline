#include "openstoryline/protocol.hpp"

#include <json-c/json.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <memory>
#include <regex>
#include <set>
#include <sstream>

namespace openstoryline::worker {
namespace {

using JsonPtr = std::unique_ptr<json_object, decltype(&json_object_put)>;

JsonPtr make_object() { return JsonPtr(json_object_new_object(), &json_object_put); }

std::string stringify(json_object* value) {
  return json_object_to_json_string_ext(value, JSON_C_TO_STRING_PLAIN);
}

void add_string(json_object* object, const char* key, const std::string& value) {
  json_object_object_add(object, key, json_object_new_string(value.c_str()));
}

void add_nullable_string(json_object* object, const char* key,
                         const std::string& value) {
  json_object_object_add(object, key,
                         value.empty() ? json_object_new_null()
                                       : json_object_new_string(value.c_str()));
}

void add_path(json_object* object, const char* key,
              const std::filesystem::path& value) {
  add_string(object, key, value.string());
}

void add_nullable_path(json_object* object, const char* key,
                       const std::filesystem::path& value) {
  json_object_object_add(object, key,
                         value.empty() ? json_object_new_null()
                                       : json_object_new_string(value.string().c_str()));
}

void add_string_array(json_object* object, const char* key,
                      const std::vector<std::string>& values) {
  json_object* array = json_object_new_array();
  for (const auto& value : values) {
    json_object_array_add(array, json_object_new_string(value.c_str()));
  }
  json_object_object_add(object, key, array);
}

void add_path_array(json_object* object, const char* key,
                    const std::vector<std::filesystem::path>& values) {
  json_object* array = json_object_new_array();
  for (const auto& value : values) {
    json_object_array_add(array, json_object_new_string(value.string().c_str()));
  }
  json_object_object_add(object, key, array);
}

void add_string_map(json_object* object, const char* key,
                    const std::map<std::string, std::string>& values) {
  json_object* mapping = json_object_new_object();
  for (const auto& [name, value] : values) {
    json_object_object_add(mapping, name.c_str(),
                           json_object_new_string(value.c_str()));
  }
  json_object_object_add(object, key, mapping);
}

json_object* input_metadata_object(const InputMetadata& metadata) {
  json_object* object = json_object_new_object();
  add_path(object, "path", metadata.path);
  json_object_object_add(object, "size_bytes",
                         json_object_new_int64(static_cast<std::int64_t>(metadata.size_bytes)));
  json_object_object_add(object, "modified_unix_seconds",
                         json_object_new_double(metadata.modified_unix_seconds));
  json_object_object_add(object, "readable", json_object_new_boolean(metadata.readable));
  add_string(object, "extension", metadata.extension);
  add_string_map(object, "stream_metadata", metadata.stream_metadata);
  return object;
}

json_object* discovery_file_object(const DiscoveryFile& file) {
  json_object* object = input_metadata_object(file.metadata);
  add_string(object, "filename", file.filename);
  json_object_object_add(
      object, "lens_marker",
      file.lens_marker.empty() ? json_object_new_null()
                               : json_object_new_string(file.lens_marker.c_str()));
  return object;
}

json_object* required(json_object* object, const char* key, json_type type) {
  json_object* value = nullptr;
  if (!json_object_object_get_ex(object, key, &value) || value == nullptr ||
      !json_object_is_type(value, type)) {
    throw WorkerException("invalid_request", std::string("missing or invalid field: ") + key,
                          ExitCode::kValidation, {{"field", key}});
  }
  return value;
}

bool optional_bool(json_object* object, const char* key, bool fallback) {
  json_object* value = nullptr;
  if (!json_object_object_get_ex(object, key, &value)) {
    return fallback;
  }
  if (!json_object_is_type(value, json_type_boolean)) {
    throw WorkerException("invalid_request", std::string("field must be boolean: ") + key,
                          ExitCode::kValidation, {{"field", key}});
  }
  return json_object_get_boolean(value);
}

std::int64_t optional_int(json_object* object, const char* key,
                          std::int64_t fallback) {
  json_object* value = nullptr;
  if (!json_object_object_get_ex(object, key, &value)) {
    return fallback;
  }
  if (!json_object_is_type(value, json_type_int)) {
    throw WorkerException("invalid_request", std::string("field must be integer: ") + key,
                          ExitCode::kValidation, {{"field", key}});
  }
  return json_object_get_int64(value);
}

std::string optional_string(json_object* object, const char* key,
                            const std::string& fallback) {
  json_object* value = nullptr;
  if (!json_object_object_get_ex(object, key, &value)) {
    return fallback;
  }
  if (!json_object_is_type(value, json_type_string)) {
    throw WorkerException("invalid_request", std::string("field must be string: ") + key,
                          ExitCode::kValidation, {{"field", key}});
  }
  return json_object_get_string(value);
}

void validate_request_keys(json_object* object) {
  static const std::set<std::string> allowed = {
      "schema_version",          "job_id",          "input_paths",
      "output_path",             "output_width",    "output_height",
      "codec",                   "bitrate_bps",      "enable_flowstate",
      "enable_direction_lock",   "stitch_type",      "enable_stitch_fusion",
      "model_root",              "overwrite"};
  json_object_object_foreach(object, key, value) {
    static_cast<void>(value);
    if (!allowed.contains(key)) {
      throw WorkerException("invalid_request", "unknown stitch request field",
                            ExitCode::kValidation, {{"field", key}});
    }
  }
}

std::string lowercase(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return value;
}

}  // namespace

WorkerException::WorkerException(std::string stable_code, std::string message,
                                 ExitCode status,
                                 std::map<std::string, std::string> context)
    : std::runtime_error(std::move(message)),
      code(std::move(stable_code)),
      exit_code(status),
      details(std::move(context)) {}

StitchRequest parse_stitch_request(const std::filesystem::path& path) {
  json_object* raw = json_object_from_file(path.string().c_str());
  JsonPtr root(raw, &json_object_put);
  if (!root || !json_object_is_type(root.get(), json_type_object)) {
    throw WorkerException("invalid_json", "stitch request is not valid JSON",
                          ExitCode::kProtocol, {{"request_path", path.string()}});
  }
  validate_request_keys(root.get());
  StitchRequest request;
  const std::string version =
      json_object_get_string(required(root.get(), "schema_version", json_type_string));
  if (version != kSchemaVersion) {
    throw WorkerException("unsupported_schema_version", "unsupported schema_version",
                          ExitCode::kProtocol, {{"schema_version", version}});
  }
  request.job_id =
      json_object_get_string(required(root.get(), "job_id", json_type_string));
  static const std::regex uuid_pattern(
      "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$");
  if (!std::regex_match(request.job_id, uuid_pattern)) {
    throw WorkerException("invalid_request", "job_id must be a UUID",
                          ExitCode::kValidation, {{"field", "job_id"}});
  }

  json_object* paths = required(root.get(), "input_paths", json_type_array);
  const std::size_t input_count = json_object_array_length(paths);
  if (input_count < 1 || input_count > 2) {
    throw WorkerException("invalid_request", "input_paths must contain one or two files",
                          ExitCode::kValidation, {{"field", "input_paths"}});
  }
  std::set<std::string> unique_paths;
  for (std::size_t index = 0; index < input_count; ++index) {
    json_object* value = json_object_array_get_idx(paths, index);
    if (!json_object_is_type(value, json_type_string)) {
      throw WorkerException("invalid_request", "input_paths values must be strings",
                            ExitCode::kValidation);
    }
    std::filesystem::path input(json_object_get_string(value));
    if (lowercase(input.extension().string()) != ".insv") {
      throw WorkerException("invalid_request", "all worker video inputs must be .insv",
                            ExitCode::kValidation, {{"path", input.string()}});
    }
    if (!unique_paths.insert(input.string()).second) {
      throw WorkerException("invalid_request", "input_paths must be unique and ordered",
                            ExitCode::kValidation, {{"path", input.string()}});
    }
    request.input_paths.push_back(std::move(input));
  }

  request.output_path = json_object_get_string(
      required(root.get(), "output_path", json_type_string));
  if (lowercase(request.output_path.extension().string()) != ".mp4") {
    throw WorkerException("invalid_request", "output_path must use .mp4",
                          ExitCode::kValidation);
  }
  request.output_width = static_cast<int>(
      optional_int(root.get(), "output_width", request.output_width));
  request.output_height = static_cast<int>(
      optional_int(root.get(), "output_height", request.output_height));
  if (request.output_width <= 0 || request.output_height <= 0 ||
      request.output_width % 2 != 0 || request.output_height % 2 != 0 ||
      request.output_width != request.output_height * 2) {
    throw WorkerException("invalid_dimensions",
                          "output dimensions must be positive, even, and exactly 2:1",
                          ExitCode::kValidation);
  }
  request.codec = lowercase(optional_string(root.get(), "codec", request.codec));
  if (request.codec != "h264" && request.codec != "h265") {
    throw WorkerException("invalid_codec", "codec must be h264 or h265",
                          ExitCode::kValidation);
  }
  request.bitrate_bps = optional_int(root.get(), "bitrate_bps", request.bitrate_bps);
  if (request.bitrate_bps <= 0 || request.bitrate_bps > 1000000000) {
    throw WorkerException("invalid_bitrate", "bitrate_bps is outside the supported range",
                          ExitCode::kValidation);
  }
  request.enable_flowstate =
      optional_bool(root.get(), "enable_flowstate", request.enable_flowstate);
  request.enable_direction_lock = optional_bool(
      root.get(), "enable_direction_lock", request.enable_direction_lock);
  request.stitch_type =
      lowercase(optional_string(root.get(), "stitch_type", request.stitch_type));
  if (request.stitch_type != "template" && request.stitch_type != "optical_flow" &&
      request.stitch_type != "dynamic" && request.stitch_type != "ai") {
    throw WorkerException("invalid_stitch_type", "unsupported stitch_type",
                          ExitCode::kValidation);
  }
  request.enable_stitch_fusion = optional_bool(
      root.get(), "enable_stitch_fusion", request.enable_stitch_fusion);
  request.overwrite = optional_bool(root.get(), "overwrite", request.overwrite);
  json_object* model_root = nullptr;
  if (json_object_object_get_ex(root.get(), "model_root", &model_root) &&
      !json_object_is_type(model_root, json_type_null)) {
    if (!json_object_is_type(model_root, json_type_string)) {
      throw WorkerException("invalid_request", "model_root must be a string or null",
                            ExitCode::kValidation);
    }
    request.model_root = json_object_get_string(model_root);
  }
  for (const auto& input : request.input_paths) {
    if (std::filesystem::absolute(input).lexically_normal() ==
        std::filesystem::absolute(request.output_path).lexically_normal()) {
      throw WorkerException("invalid_request", "output_path cannot overwrite an input",
                            ExitCode::kValidation);
    }
  }
  return request;
}

std::string capability_json(const CapabilityResponse& response) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_string(root.get(), "worker_version", response.worker_version);
  add_string(root.get(), "provider", response.provider);
  json_object_object_add(root.get(), "sdk_enabled",
                         json_object_new_boolean(response.sdk_enabled));
  add_nullable_string(root.get(), "sdk_version", response.sdk_version);
  json_object_object_add(root.get(), "can_decode_insv",
                         json_object_new_boolean(response.can_decode_insv));
  json_object_object_add(root.get(), "can_stitch",
                         json_object_new_boolean(response.can_stitch));
  json_object_object_add(root.get(), "supports_progress",
                         json_object_new_boolean(response.supports_progress));
  json_object_object_add(root.get(), "supports_cancellation",
                         json_object_new_boolean(response.supports_cancellation));
  add_string_array(root.get(), "supported_codecs", response.supported_codecs);
  add_string_array(root.get(), "supported_stitch_types",
                   response.supported_stitch_types);
  json_object_object_add(root.get(), "max_input_files",
                         json_object_new_int(response.max_input_files));
  json_object_object_add(root.get(), "requires_gpu",
                         json_object_new_boolean(response.requires_gpu));
  add_string(root.get(), "target_platform", response.target_platform);
  json_object_object_add(root.get(), "model_root_configured",
                         json_object_new_boolean(response.model_root_configured));
  add_string_array(root.get(), "warnings", response.warnings);
  return stringify(root.get());
}

std::string probe_json(const ProbeResponse& response) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_string(root.get(), "provider", response.provider);
  json_object_object_add(root.get(), "decodable",
                         json_object_new_boolean(response.decodable));
  json_object* inputs = json_object_new_array();
  for (const auto& input : response.inputs) {
    json_object_array_add(inputs, input_metadata_object(input));
  }
  json_object_object_add(root.get(), "inputs", inputs);
  add_nullable_string(root.get(), "camera_model", response.camera_model);
  add_string_array(root.get(), "warnings", response.warnings);
  return stringify(root.get());
}

std::string discovery_json(const DiscoveryResponse& response) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_path(root.get(), "directory", response.directory);
  json_object* groups = json_object_new_array();
  for (const auto& group : response.groups) {
    json_object* object = json_object_new_object();
    add_string(object, "group_id", group.group_id);
    json_object* files = json_object_new_array();
    for (const auto& file : group.files) {
      json_object_array_add(files, discovery_file_object(file));
    }
    json_object_object_add(object, "files", files);
    add_path_array(object, "proposed_order", group.proposed_order);
    json_object_object_add(object, "confidence",
                           json_object_new_double(group.confidence));
    add_string_array(object, "reasons", group.reasons);
    add_string_array(object, "warnings", group.warnings);
    json_object_object_add(object, "requires_confirmation",
                           json_object_new_boolean(group.requires_confirmation));
    json_object_array_add(groups, object);
  }
  json_object_object_add(root.get(), "groups", groups);
  json_object* ungrouped = json_object_new_array();
  for (const auto& file : response.ungrouped) {
    json_object_array_add(ungrouped, discovery_file_object(file));
  }
  json_object_object_add(root.get(), "ungrouped", ungrouped);
  add_string_array(root.get(), "warnings", response.warnings);
  return stringify(root.get());
}

std::string progress_json(const std::string& job_id, int progress,
                          const std::string& message) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_string(root.get(), "event", "progress");
  add_string(root.get(), "job_id", job_id);
  json_object_object_add(root.get(), "progress_percent",
                         json_object_new_int(std::clamp(progress, 0, 100)));
  add_string(root.get(), "message", message);
  add_string(root.get(), "created_at", now_utc_iso8601());
  return stringify(root.get());
}

std::string result_json(const StitchResult& result) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_string(root.get(), "event", "result");
  add_string(root.get(), "job_id", result.job_id);
  add_string(root.get(), "provider", result.provider);
  add_path_array(root.get(), "input_paths", result.input_paths);
  add_string_array(root.get(), "source_sha256", result.source_sha256);
  add_path(root.get(), "output_path", result.output_path);
  add_string(root.get(), "output_sha256", result.output_sha256);
  json_object_object_add(root.get(), "output_width",
                         json_object_new_int(result.output_width));
  json_object_object_add(root.get(), "output_height",
                         json_object_new_int(result.output_height));
  add_string(root.get(), "codec", result.codec);
  json_object_object_add(root.get(), "bitrate_bps",
                         json_object_new_int64(result.bitrate_bps));
  json_object_object_add(root.get(), "enable_flowstate",
                         json_object_new_boolean(result.enable_flowstate));
  json_object_object_add(root.get(), "enable_direction_lock",
                         json_object_new_boolean(result.enable_direction_lock));
  add_string(root.get(), "stitch_type", result.stitch_type);
  json_object_object_add(root.get(), "enable_stitch_fusion",
                         json_object_new_boolean(result.enable_stitch_fusion));
  json_object_object_add(root.get(), "elapsed_seconds",
                         json_object_new_double(result.elapsed_seconds));
  add_nullable_path(root.get(), "sdk_log_path", result.sdk_log_path);
  add_string_array(root.get(), "warnings", result.warnings);
  return stringify(root.get());
}

std::string error_json(const WorkerException& error, const std::string& job_id) {
  auto root = make_object();
  add_string(root.get(), "schema_version", kSchemaVersion);
  add_string(root.get(), "event", "error");
  add_string(root.get(), "code", error.code);
  add_string(root.get(), "message", error.what());
  add_string_map(root.get(), "details", error.details);
  json_object_object_add(root.get(), "retryable", json_object_new_boolean(false));
  add_nullable_string(root.get(), "job_id", job_id);
  return stringify(root.get());
}

std::string now_utc_iso8601() {
  const auto now = std::chrono::system_clock::now();
  const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
                                now.time_since_epoch()) %
                            1000;
  const std::time_t time = std::chrono::system_clock::to_time_t(now);
  std::tm utc{};
#ifdef _WIN32
  gmtime_s(&utc, &time);
#else
  gmtime_r(&time, &utc);
#endif
  std::ostringstream output;
  output << std::put_time(&utc, "%Y-%m-%dT%H:%M:%S") << '.' << std::setw(3)
         << std::setfill('0') << milliseconds.count() << 'Z';
  return output.str();
}

}  // namespace openstoryline::worker
