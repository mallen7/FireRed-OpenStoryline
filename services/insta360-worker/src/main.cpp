#include "openstoryline/discovery.hpp"
#include "openstoryline/provider.hpp"
#include "openstoryline/protocol.hpp"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <csignal>
#include <filesystem>
#include <iostream>
#include <string>
#include <vector>

namespace worker = openstoryline::worker;

namespace {

std::atomic_bool g_cancelled{false};

void handle_signal(int signal_number) {
  static_cast<void>(signal_number);
  g_cancelled.store(true);
}

std::string option_value(const std::vector<std::string>& arguments,
                         const std::string& name) {
  const auto match = std::find(arguments.begin(), arguments.end(), name);
  if (match == arguments.end() || std::next(match) == arguments.end()) {
    throw worker::WorkerException("invalid_arguments", "missing required CLI option",
                                  worker::ExitCode::kUsage, {{"option", name}});
  }
  return *std::next(match);
}

std::vector<std::string> option_values(const std::vector<std::string>& arguments,
                                       const std::string& name) {
  std::vector<std::string> values;
  for (std::size_t index = 0; index < arguments.size(); ++index) {
    if (arguments[index] == name) {
      if (index + 1 >= arguments.size()) {
        throw worker::WorkerException("invalid_arguments", "CLI option has no value",
                                      worker::ExitCode::kUsage, {{"option", name}});
      }
      values.push_back(arguments[++index]);
    }
  }
  return values;
}

bool has_insv_extension(const std::filesystem::path& path) {
  auto extension = path.extension().string();
  std::transform(extension.begin(), extension.end(), extension.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return extension == ".insv";
}

}  // namespace

int main(int argc, char* argv[]) {
  std::signal(SIGINT, handle_signal);
  std::signal(SIGTERM, handle_signal);
  std::string job_id;
  try {
    if (argc < 2) {
      throw worker::WorkerException(
          "invalid_arguments",
          "usage: insta360-worker capabilities|discover|probe|stitch",
          worker::ExitCode::kUsage);
    }
    const std::string command = argv[1];
    std::vector<std::string> arguments;
    for (int index = 2; index < argc; ++index) {
      arguments.emplace_back(argv[index]);
    }
    auto provider = worker::make_provider();
    std::cerr << "insta360-worker: command=" << command
              << " provider=" << provider->capabilities().provider << '\n';

    if (command == "capabilities") {
      std::cout << worker::capability_json(provider->capabilities()) << std::endl;
      return static_cast<int>(worker::ExitCode::kSuccess);
    }
    if (command == "discover") {
      const auto directory = option_value(arguments, "--directory");
      std::cout << worker::discovery_json(worker::discover_inputs(directory))
                << std::endl;
      return static_cast<int>(worker::ExitCode::kSuccess);
    }
    if (command == "probe") {
      const auto values = option_values(arguments, "--input");
      if (values.empty() || values.size() > 2) {
        throw worker::WorkerException(
            "invalid_arguments", "probe requires one or two ordered --input values",
            worker::ExitCode::kUsage);
      }
      worker::ProbeRequest request;
      for (const auto& value : values) {
        if (!has_insv_extension(value)) {
          throw worker::WorkerException(
              "invalid_arguments", "probe inputs must use the .insv extension",
              worker::ExitCode::kUsage, {{"input", value}});
        }
        request.input_paths.emplace_back(value);
      }
      std::cout << worker::probe_json(provider->probe(request)) << std::endl;
      return static_cast<int>(worker::ExitCode::kSuccess);
    }
    if (command == "stitch") {
      const auto request_path = option_value(arguments, "--request");
      const auto request = worker::parse_stitch_request(request_path);
      job_id = request.job_id;
      const auto progress = [&](int percent, const std::string& message) {
        std::cout << worker::progress_json(job_id, percent, message) << std::endl;
      };
      const auto result = provider->stitch(request, progress, g_cancelled);
      if (g_cancelled.load()) {
        throw worker::WorkerException("cancelled", "stitch job was cancelled",
                                      worker::ExitCode::kCancelled);
      }
      std::cout << worker::result_json(result) << std::endl;
      return static_cast<int>(worker::ExitCode::kSuccess);
    }
    throw worker::WorkerException("unknown_command", "unknown worker command",
                                  worker::ExitCode::kUsage, {{"command", command}});
  } catch (const worker::WorkerException& error) {
    std::cerr << "insta360-worker: " << error.code << ": " << error.what() << '\n';
    std::cout << worker::error_json(error, job_id) << std::endl;
    return static_cast<int>(error.exit_code);
  } catch (const std::exception& error) {
    const worker::WorkerException wrapped(
        "internal_error", "worker encountered an unexpected internal error",
        worker::ExitCode::kProtocol, {{"exception", error.what()}});
    std::cerr << "insta360-worker: internal_error: " << error.what() << '\n';
    std::cout << worker::error_json(wrapped, job_id) << std::endl;
    return static_cast<int>(wrapped.exit_code);
  }
}
