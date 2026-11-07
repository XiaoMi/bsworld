# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Links the platform Dockerfiles to the stages they share in base.Dockerfile.
# Build one platform image from the repository root:
#
#   docker buildx bake -f docker/docker-bake.hcl zephyr
#
# Each variable below can be set from the environment of that command.

variable "BUILD_UID" {
  default = "1000"
}

variable "BUILD_GID" {
  default = "1000"
}

variable "BUILD_JOBS" {
  default = "4"
}

# Git metadata is excluded from images; CI supplies the Git-derived version.
variable "BSWORLD_VERSION" {
  default = "0+local"
}

# Platform images are tagged bsworld-<platform>:<TAG>.
variable "TAG" {
  default = "dev"
}

# Recorded as OCI labels when set, so a published image names its commit.
variable "SOURCE" {
  default = ""
}

variable "REVISION" {
  default = ""
}

# A registry repository for the build cache; each target uses its own tag.
variable "CACHE_REPOSITORY" {
  default = ""
}

# Only builds that publish refresh the cache; others only read it.
variable "CACHE_WRITE" {
  default = "false"
}

function "cache_from" {
  params = [name]
  result = CACHE_REPOSITORY == "" ? [] : ["type=registry,ref=${CACHE_REPOSITORY}:${name}"]
}

function "cache_to" {
  params = [name]
  result = CACHE_REPOSITORY != "" && CACHE_WRITE == "true" ? ["type=registry,ref=${CACHE_REPOSITORY}:${name},mode=max"] : []
}

group "default" {
  targets = ["zephyr", "nuttx", "trouble", "linux"]
}

target "_common" {
  context   = "."
  platforms = ["linux/amd64"]
  args = {
    BUILD_UID       = BUILD_UID
    BUILD_GID       = BUILD_GID
    BUILD_JOBS      = BUILD_JOBS
    BSWORLD_VERSION = BSWORLD_VERSION
  }
}

target "_platform" {
  inherits = ["_common"]
  labels = merge(
    SOURCE == "" ? {} : { "org.opencontainers.image.source" = SOURCE },
    REVISION == "" ? {} : { "org.opencontainers.image.revision" = REVISION },
  )
}

# Stages of base.Dockerfile that the platform Dockerfiles build on.

target "build-base" {
  inherits   = ["_common"]
  dockerfile = "docker/base.Dockerfile"
  target     = "build-base"
  cache-from = cache_from("build-base")
  cache-to   = cache_to("build-base")
}

target "babblesim" {
  inherits   = ["_common"]
  dockerfile = "docker/base.Dockerfile"
  target     = "babblesim"
  cache-from = cache_from("babblesim")
  cache-to   = cache_to("babblesim")
}

target "runtime-base" {
  inherits   = ["_common"]
  dockerfile = "docker/base.Dockerfile"
  target     = "runtime-base"
  cache-from = cache_from("runtime-base")
  cache-to   = cache_to("runtime-base")
}

# Platform images, one per CI workflow.

target "zephyr" {
  inherits   = ["_platform"]
  dockerfile = "docker/zephyr.Dockerfile"
  target     = "runtime-zephyr"
  contexts = {
    babblesim    = "target:babblesim"
    runtime-base = "target:runtime-base"
  }
  tags       = ["bsworld-zephyr:${TAG}"]
  cache-from = cache_from("zephyr")
  cache-to   = cache_to("zephyr")
}

target "nuttx" {
  inherits   = ["_platform"]
  dockerfile = "docker/nuttx.Dockerfile"
  target     = "runtime-nuttx"
  contexts = {
    babblesim    = "target:babblesim"
    runtime-base = "target:runtime-base"
  }
  tags       = ["bsworld-nuttx:${TAG}"]
  cache-from = cache_from("nuttx")
  cache-to   = cache_to("nuttx")
}

target "trouble" {
  inherits   = ["_platform"]
  dockerfile = "docker/trouble.Dockerfile"
  target     = "runtime-trouble"
  contexts = {
    babblesim    = "target:babblesim"
    runtime-base = "target:runtime-base"
  }
  tags       = ["bsworld-trouble:${TAG}"]
  cache-from = cache_from("trouble")
  cache-to   = cache_to("trouble")
}

target "linux" {
  inherits   = ["_platform"]
  dockerfile = "docker/linux.Dockerfile"
  target     = "runtime-linux"
  contexts = {
    build-base   = "target:build-base"
    runtime-base = "target:runtime-base"
  }
  tags       = ["bsworld-linux:${TAG}"]
  cache-from = cache_from("linux")
  cache-to   = cache_to("linux")
}
