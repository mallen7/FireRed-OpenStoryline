cmake_minimum_required(VERSION 3.20)
project(openstoryline_insta360_worker VERSION 1.0.0 LANGUAGES CXX)

option(INSTA360_ENABLE_SDK "Build the proprietary MediaSDK provider" OFF)
option(INSTA360_SDK_HAS_MODEL_ROOT_API "MediaSDK exposes SetModelFileRootDir" ON)
option(INSTA360_SDK_HAS_STITCH_FUSION "MediaSDK exposes EnableStitchFusion" ON)
set(INSTA360_SDK_ROOT "$ENV{INSTA360_SDK_ROOT}" CACHE PATH "Operator-supplied MediaSDK root")
set(INSTA360_MODEL_ROOT "$ENV{INSTA360_MODEL_ROOT}" CACHE PATH "Operator-supplied model root")
set(INSTA360_SDK_LIBRARY "" CACHE FILEPATH "Explicit MediaSDK library when auto-discovery is insufficient")
set(INSTA360_SDK_VERSION "operator-supplied" CACHE STRING "MediaSDK version reported by capabilities")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_path(JSON_C_INCLUDE_DIR json-c/json.h
  HINTS /opt/homebrew/include /usr/local/include /usr/include)
find_library(JSON_C_LIBRARY NAMES json-c
  HINTS /opt/homebrew/lib /usr/local/lib /usr/lib /usr/lib/x86_64-linux-gnu)
if(NOT JSON_C_INCLUDE_DIR OR NOT JSON_C_LIBRARY)
  message(FATAL_ERROR "json-c development files are required (Ubuntu: apt install libjson-c-dev)")
endif()

add_executable(insta360-worker
  src/discovery.cpp
  src/main.cpp
  src/protocol.cpp
  src/provider_stub.cpp
  src/sha256.cpp)
target_include_directories(insta360-worker PRIVATE include)
target_include_directories(insta360-worker SYSTEM PRIVATE "${JSON_C_INCLUDE_DIR}")
target_link_libraries(insta360-worker PRIVATE "${JSON_C_LIBRARY}")
target_compile_definitions(insta360-worker PRIVATE
  OPENSTORYLINE_WORKER_VERSION="${PROJECT_VERSION}")
if(MSVC)
  target_compile_options(insta360-worker PRIVATE /W4 /permissive-)
else()
  target_compile_options(insta360-worker PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

if(INSTA360_ENABLE_SDK)
  if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux" OR NOT CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|amd64|AMD64")
    message(FATAL_ERROR "The MediaSDK provider target is Ubuntu 22.04 x86-64; this host is unsupported")
  endif()
  if(NOT INSTA360_SDK_ROOT OR NOT IS_DIRECTORY "${INSTA360_SDK_ROOT}")
    message(FATAL_ERROR "INSTA360_ENABLE_SDK=ON requires an existing INSTA360_SDK_ROOT")
  endif()
  find_path(INSTA360_SDK_INCLUDE_DIR ins_stitcher.h
    PATHS "${INSTA360_SDK_ROOT}/include" "${INSTA360_SDK_ROOT}/usr/include"
    NO_DEFAULT_PATH)
  if(NOT INSTA360_SDK_INCLUDE_DIR)
    message(FATAL_ERROR "ins_stitcher.h was not found beneath INSTA360_SDK_ROOT")
  endif()
  if(NOT INSTA360_SDK_LIBRARY)
    find_library(INSTA360_SDK_LIBRARY
      NAMES MediaSDK media_sdk Insta360MediaSDK
      PATHS "${INSTA360_SDK_ROOT}/lib" "${INSTA360_SDK_ROOT}/lib64"
            "${INSTA360_SDK_ROOT}/usr/lib" "${INSTA360_SDK_ROOT}/usr/lib/x86_64-linux-gnu"
      NO_DEFAULT_PATH)
  endif()
  if(NOT INSTA360_SDK_LIBRARY OR NOT EXISTS "${INSTA360_SDK_LIBRARY}")
    message(FATAL_ERROR "MediaSDK library was not found; set INSTA360_SDK_LIBRARY explicitly")
  endif()
  if(INSTA360_MODEL_ROOT AND NOT IS_DIRECTORY "${INSTA360_MODEL_ROOT}")
    message(FATAL_ERROR "INSTA360_MODEL_ROOT was provided but is not a directory")
  endif()

  target_sources(insta360-worker PRIVATE src/provider_sdk.cpp)
  target_include_directories(insta360-worker PRIVATE "${INSTA360_SDK_INCLUDE_DIR}")
  target_link_libraries(insta360-worker PRIVATE "${INSTA360_SDK_LIBRARY}")
  target_compile_definitions(insta360-worker PRIVATE
    INSTA360_ENABLE_SDK=1
    INSTA360_SDK_VERSION="${INSTA360_SDK_VERSION}"
    INSTA360_DEFAULT_MODEL_ROOT="${INSTA360_MODEL_ROOT}")
  if(INSTA360_SDK_HAS_MODEL_ROOT_API)
    target_compile_definitions(insta360-worker PRIVATE INSTA360_SDK_HAS_MODEL_ROOT_API=1)
  endif()
  if(INSTA360_SDK_HAS_STITCH_FUSION)
    target_compile_definitions(insta360-worker PRIVATE INSTA360_SDK_HAS_STITCH_FUSION=1)
  endif()
endif()

include(CTest)
if(BUILD_TESTING)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  add_test(
    NAME insta360_worker_protocol
    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_worker_cli.py"
            "$<TARGET_FILE:insta360-worker>")
  add_executable(insta360-worker-sha256-test
    tests/test_sha256.cpp
    src/protocol.cpp
    src/sha256.cpp)
  target_include_directories(insta360-worker-sha256-test PRIVATE include)
  target_include_directories(insta360-worker-sha256-test SYSTEM PRIVATE
    "${JSON_C_INCLUDE_DIR}")
  target_link_libraries(insta360-worker-sha256-test PRIVATE "${JSON_C_LIBRARY}")
  if(MSVC)
    target_compile_options(insta360-worker-sha256-test PRIVATE /W4 /permissive-)
  else()
    target_compile_options(insta360-worker-sha256-test PRIVATE
      -Wall -Wextra -Wpedantic -Werror)
  endif()
  add_test(NAME insta360_worker_sha256 COMMAND insta360-worker-sha256-test)

  # Compile the private adapter against a tiny public test double that mirrors
  # only the API calls documented in Insta360's official Linux example. This
  # catches integration-source regressions without distributing SDK material.
  add_executable(insta360-worker-sdk-adapter-compile-test
    tests/test_sdk_adapter_compile.cpp
    src/discovery.cpp
    src/protocol.cpp
    src/provider_sdk.cpp
    src/sha256.cpp)
  target_include_directories(insta360-worker-sdk-adapter-compile-test PRIVATE
    include tests)
  target_include_directories(insta360-worker-sdk-adapter-compile-test SYSTEM PRIVATE
    "${JSON_C_INCLUDE_DIR}")
  target_link_libraries(insta360-worker-sdk-adapter-compile-test PRIVATE
    "${JSON_C_LIBRARY}")
  target_compile_definitions(insta360-worker-sdk-adapter-compile-test PRIVATE
    INSTA360_ENABLE_SDK=1
    OPENSTORYLINE_MOCK_INSTA360_SDK=1
    INSTA360_SDK_HAS_MODEL_ROOT_API=1
    INSTA360_SDK_HAS_STITCH_FUSION=1
    OPENSTORYLINE_WORKER_VERSION="${PROJECT_VERSION}"
    INSTA360_SDK_VERSION="official-api-test-double"
    INSTA360_DEFAULT_MODEL_ROOT="")
  if(MSVC)
    target_compile_options(insta360-worker-sdk-adapter-compile-test PRIVATE
      /W4 /permissive-)
  else()
    target_compile_options(insta360-worker-sdk-adapter-compile-test PRIVATE
      -Wall -Wextra -Wpedantic -Werror)
  endif()
  add_test(NAME insta360_worker_sdk_adapter_compile
    COMMAND insta360-worker-sdk-adapter-compile-test)
endif()
