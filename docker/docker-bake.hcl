# Copyright 2026 Xiaomi Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Build the UML test image from the repository root:
#
#   docker buildx bake -f docker/docker-bake.hcl
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

# The UML image is tagged bsworld-linux:<TAG>.
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
  targets = ["linux"]
}

target "linux" {
  context    = "."
  dockerfile = "docker/linux.Dockerfile"
  target     = "runtime-linux"
  platforms  = ["linux/amd64"]
  args = {
    BUILD_UID       = BUILD_UID
    BUILD_GID       = BUILD_GID
    BUILD_JOBS      = BUILD_JOBS
    BSWORLD_VERSION = BSWORLD_VERSION
  }
  labels = merge(
    SOURCE == "" ? {} : { "org.opencontainers.image.source" = SOURCE },
    REVISION == "" ? {} : { "org.opencontainers.image.revision" = REVISION },
  )
  tags       = ["bsworld-linux:${TAG}"]
  cache-from = cache_from("linux")
  cache-to   = cache_to("linux")
}
