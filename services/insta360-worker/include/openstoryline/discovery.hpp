#pragma once

#include <filesystem>

#include "openstoryline/protocol.hpp"

namespace openstoryline::worker {

InputMetadata inspect_file(const std::filesystem::path& path);
DiscoveryResponse discover_inputs(const std::filesystem::path& directory);

}  // namespace openstoryline::worker
