#include "openstoryline/discovery.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <fstream>
#include <map>
#include <regex>

namespace openstoryline::worker {
namespace {

std::string lowercase(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return value;
}

double modified_seconds(const std::filesystem::path& path) {
  const auto file_time = std::filesystem::last_write_time(path);
  const auto system_time = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
      file_time - std::filesystem::file_time_type::clock::now() +
      std::chrono::system_clock::now());
  return std::chrono::duration<double>(system_time.time_since_epoch()).count();
}

DiscoveryFile make_discovery_file(const std::filesystem::path& path,
                                  const std::string& marker = {}) {
  return DiscoveryFile{inspect_file(path), path.filename().string(), marker};
}

}  // namespace

InputMetadata inspect_file(const std::filesystem::path& path) {
  if (!std::filesystem::is_regular_file(path)) {
    throw WorkerException("input_not_found", "input is not a regular file",
                          ExitCode::kValidation, {{"path", path.string()}});
  }
  std::ifstream stream(path, std::ios::binary);
  return InputMetadata{
      std::filesystem::absolute(path).lexically_normal(),
      std::filesystem::file_size(path),
      modified_seconds(path),
      stream.good(),
      lowercase(path.extension().string()),
      {},
  };
}

DiscoveryResponse discover_inputs(const std::filesystem::path& directory) {
  if (!std::filesystem::is_directory(directory)) {
    throw WorkerException("directory_not_found", "discovery directory does not exist",
                          ExitCode::kValidation,
                          {{"directory", directory.string()}});
  }
  const auto resolved = std::filesystem::absolute(directory).lexically_normal();
  std::vector<std::filesystem::path> inputs;
  for (const auto& entry : std::filesystem::directory_iterator(resolved)) {
    if (entry.is_regular_file() &&
        lowercase(entry.path().extension().string()) == ".insv") {
      inputs.push_back(entry.path());
    }
  }
  std::sort(inputs.begin(), inputs.end(), [](const auto& left, const auto& right) {
    return left.filename().string() < right.filename().string();
  });

  const std::regex paired_name("^(.*)_(00|10)_([^.]*)$",
                               std::regex::icase);
  std::map<std::string, std::vector<DiscoveryFile>> candidates;
  std::vector<DiscoveryFile> single_track;
  for (const auto& path : inputs) {
    std::smatch match;
    const std::string stem = path.stem().string();
    if (std::regex_match(stem, match, paired_name)) {
      const std::string marker = match[2].str();
      const std::string key = lowercase(match[1].str() + "_" + match[3].str());
      candidates[key].push_back(make_discovery_file(path, marker));
    } else {
      single_track.push_back(make_discovery_file(path));
    }
  }

  DiscoveryResponse response;
  response.directory = resolved;
  for (auto& [key, files] : candidates) {
    std::sort(files.begin(), files.end(), [](const auto& left, const auto& right) {
      return left.lens_marker < right.lens_marker;
    });
    if (files.size() == 1) {
      DiscoveryGroup group;
      group.group_id = key;
      group.files = files;
      group.proposed_order = {files[0].metadata.path};
      group.confidence = 0.35;
      group.reasons.push_back(
          "One marker-bearing .insv file was found; current cameras may store both lens streams in one container.");
      group.warnings.push_back(
          "This is ambiguous: the file may be a complete single-container recording or one half of a legacy pair.");
      group.warnings.push_back(
          "Confirm the camera model and file completeness before stitching.");
      group.warnings.push_back(
          "Stream metadata is unavailable without a decoding MediaSDK provider.");
      response.groups.push_back(std::move(group));
      continue;
    }
    if (files.size() != 2 || files[0].lens_marker == files[1].lens_marker) {
      response.ungrouped.insert(response.ungrouped.end(), files.begin(), files.end());
      response.warnings.push_back(
          "A filename group did not contain one _00_ and one _10_ file; no pair was proposed.");
      continue;
    }
    DiscoveryGroup group;
    group.group_id = key;
    group.files = files;
    group.proposed_order = {files[0].metadata.path, files[1].metadata.path};
    group.confidence = 0.75;
    group.reasons.push_back("Filenames share capture evidence around complementary _00_/_10_ lens markers.");
    const double timestamp_delta = std::abs(files[0].metadata.modified_unix_seconds -
                                            files[1].metadata.modified_unix_seconds);
    if (timestamp_delta <= 5.0) {
      group.confidence += 0.15;
      group.reasons.push_back("Filesystem timestamps are within five seconds.");
    } else {
      group.warnings.push_back("Filesystem timestamps differ by more than five seconds.");
    }
    const auto smaller = std::min(files[0].metadata.size_bytes,
                                  files[1].metadata.size_bytes);
    const auto larger = std::max(files[0].metadata.size_bytes,
                                 files[1].metadata.size_bytes);
    const double size_ratio = larger == 0 ? 0.0 : static_cast<double>(smaller) /
                                                   static_cast<double>(larger);
    if (size_ratio >= 0.70) {
      group.confidence += 0.10;
      group.reasons.push_back("File sizes are within thirty percent.");
    } else {
      group.warnings.push_back("File sizes differ substantially.");
    }
    group.confidence = std::min(1.0, group.confidence);
    group.warnings.push_back(
        "Stream metadata is unavailable without a decoding MediaSDK provider.");
    response.groups.push_back(std::move(group));
  }

  for (auto& file : single_track) {
    DiscoveryGroup group;
    group.group_id = lowercase(file.metadata.path.stem().string());
    group.files.push_back(file);
    group.proposed_order.push_back(file.metadata.path);
    group.confidence = 0.50;
    group.reasons.push_back(
        "No paired lens marker was present; this may be a supported single-track camera file.");
    group.warnings.push_back(
        "Confirm the camera model and file completeness before stitching.");
    group.warnings.push_back(
        "Stream metadata is unavailable without a decoding MediaSDK provider.");
    response.groups.push_back(std::move(group));
  }
  if (inputs.empty()) {
    response.warnings.push_back("No .insv files were found in the directory.");
  }
  return response;
}

}  // namespace openstoryline::worker
