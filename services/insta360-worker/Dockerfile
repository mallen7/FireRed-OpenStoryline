FROM ubuntu:22.04 AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake \
    g++ \
    libjson-c-dev \
    ninja-build \
    python3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . .
RUN cmake -S . -B build -G Ninja -DINSTA360_ENABLE_SDK=OFF -DBUILD_TESTING=ON \
 && cmake --build build --parallel 2 \
 && ctest --test-dir build --output-on-failure

FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends libjson-c5 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/insta360-worker /usr/local/bin/insta360-worker
ENTRYPOINT ["/usr/local/bin/insta360-worker"]
