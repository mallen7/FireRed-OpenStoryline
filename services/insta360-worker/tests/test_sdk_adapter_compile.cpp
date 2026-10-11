#include "openstoryline/provider.hpp"

int main() {
  openstoryline::worker::Insta360SdkStitchProvider provider;
  const auto capability = provider.capabilities();
  return capability.sdk_enabled && capability.can_stitch ? 0 : 1;
}
