#pragma once

#include <filesystem>
#include <string>

namespace openstoryline::worker {

std::string sha256_file(const std::filesystem::path& path);

}  // namespace openstoryline::worker
