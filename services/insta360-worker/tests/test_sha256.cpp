#include "openstoryline/sha256.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>

int main() {
  const auto path = std::filesystem::temp_directory_path() /
                    "openstoryline-insta360-sha256-test.txt";
  {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output << "abc";
  }
  const std::string actual = openstoryline::worker::sha256_file(path);
  std::error_code error;
  std::filesystem::remove(path, error);
  const std::string expected =
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
  if (actual != expected) {
    std::cerr << "SHA-256 mismatch: " << actual << '\n';
    return 1;
  }
  return 0;
}
