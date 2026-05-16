cmake_minimum_required(VERSION 3.15)
project(OpenFaultSim LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Fetch Catch2 for testing
include(FetchContent)
FetchContent_Declare(
  Catch2
  GIT_REPOSITORY https://github.com/catchorg/Catch2.git
  GIT_TAG        v3.4.0
)
FetchContent_MakeAvailable(Catch2)

# Include directories
include_directories(${CMAKE_SOURCE_DIR}/src)

# Tests
enable_testing()
add_subdirectory(tests/cpp)
