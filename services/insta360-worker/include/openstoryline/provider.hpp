#pragma once

#include <atomic>
#include <functional>
#include <memory>

#include "openstoryline/protocol.hpp"

namespace openstoryline::worker {

using ProgressCallback = std::function<void(int, const std::string&)>;

class StitchProvider {
 public:
  virtual ~StitchProvider() = default;
  virtual CapabilityResponse capabilities() const = 0;
  virtual ProbeResponse probe(const ProbeRequest& request) const = 0;
  virtual StitchResult stitch(const StitchRequest& request,
                              const ProgressCallback& progress,
                              const std::atomic_bool& cancelled) = 0;
};

class StubStitchProvider final : public StitchProvider {
 public:
  CapabilityResponse capabilities() const override;
  ProbeResponse probe(const ProbeRequest& request) const override;
  StitchResult stitch(const StitchRequest& request,
                      const ProgressCallback& progress,
                      const std::atomic_bool& cancelled) override;
};

#ifdef INSTA360_ENABLE_SDK
class Insta360SdkStitchProvider final : public StitchProvider {
 public:
  Insta360SdkStitchProvider();
  CapabilityResponse capabilities() const override;
  ProbeResponse probe(const ProbeRequest& request) const override;
  StitchResult stitch(const StitchRequest& request,
                      const ProgressCallback& progress,
                      const std::atomic_bool& cancelled) override;
};
#endif

std::unique_ptr<StitchProvider> make_provider();

}  // namespace openstoryline::worker
