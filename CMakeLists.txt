cmake_minimum_required(VERSION 3.20)
project(faultflow LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Parallel fault grading uses std::thread; link the platform threads library.
set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

# Opt-in sanitizer for the multithreaded grading code. Off by default so normal
# builds are unaffected. Configure with -DFAULTFLOW_SANITIZER=tsan|asan.
set(FAULTFLOW_SANITIZER "" CACHE STRING "Sanitizer: tsan | asan | '' (none)")
if(FAULTFLOW_SANITIZER STREQUAL "tsan")
  add_compile_options(-fsanitize=thread -g -O1 -fno-omit-frame-pointer)
  add_link_options(-fsanitize=thread)
elseif(FAULTFLOW_SANITIZER STREQUAL "asan")
  add_compile_options(-fsanitize=address,undefined -g -O1 -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
elseif(NOT FAULTFLOW_SANITIZER STREQUAL "")
  message(FATAL_ERROR "FAULTFLOW_SANITIZER must be tsan, asan, or empty")
endif()

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
