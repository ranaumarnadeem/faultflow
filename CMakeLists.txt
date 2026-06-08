cmake_minimum_required(VERSION 3.20)
project(faultflow LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

FetchContent_Declare(
  Catch2
  GIT_REPOSITORY https://github.com/catchorg/Catch2.git
  GIT_TAG v3.4.0
)
FetchContent_Declare(
  json
  GIT_REPOSITORY https://github.com/nlohmann/json.git
  GIT_TAG v3.11.3
)
FetchContent_MakeAvailable(Catch2 json)

find_package(SQLiteCpp REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

add_subdirectory(src/core)
enable_testing()
add_subdirectory(tests/cpp)
