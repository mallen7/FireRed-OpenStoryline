#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace ins {

enum class InsLogLevel { ERR };
enum class STITCH_TYPE { TEMPLATE, OPTFLOW, DYNAMICSTITCH, AIFLOW };

inline void SetLogLevel(InsLogLevel level) { static_cast<void>(level); }
inline void InitEnv() {}
inline void SetLogPath(const std::string& path) { static_cast<void>(path); }
inline void SetModelFileRootDir(const std::string& path) {
  static_cast<void>(path);
}

class VideoStitcher {
 public:
  void SetInputPath(std::vector<std::string>& paths) { static_cast<void>(paths); }
  void SetOutputPath(const std::string& path) { static_cast<void>(path); }
  void SetOutputSize(int width, int height) {
    static_cast<void>(width);
    static_cast<void>(height);
  }
  void SetOutputBitRate(std::int64_t bitrate) { static_cast<void>(bitrate); }
  void SetStitchType(STITCH_TYPE type) { static_cast<void>(type); }
  void EnableFlowState(bool enabled) { static_cast<void>(enabled); }
  void EnableDirectionLock(bool enabled) { static_cast<void>(enabled); }
  void EnableStitchFusion(bool enabled) { static_cast<void>(enabled); }
  void EnableH265Encoder() {}
  void SetStitchProgressCallback(std::function<void(int, int)> callback) {
    progress_ = std::move(callback);
  }
  void SetStitchStateCallback(
      std::function<void(int, const char*)> callback) {
    state_ = std::move(callback);
  }
  void StartStitch() {}
  bool CancelStitch() { return true; }

 private:
  std::function<void(int, int)> progress_;
  std::function<void(int, const char*)> state_;
};

}  // namespace ins
