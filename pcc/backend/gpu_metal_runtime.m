#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dispatch/dispatch.h>
#include <mach-o/dyld.h>
#include <mach-o/getsect.h>
#include <mach-o/ldsyms.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int pcc_metal_default_library_path(char *out, size_t out_size) {
  uint32_t size = 0;
  _NSGetExecutablePath(NULL, &size);
  if (size == 0) {
    return -1;
  }
  char *raw = (char *)malloc(size);
  if (raw == NULL) {
    return -1;
  }
  if (_NSGetExecutablePath(raw, &size) != 0) {
    free(raw);
    return -1;
  }
  char *resolved = realpath(raw, NULL);
  free(raw);
  if (resolved == NULL) {
    return -1;
  }
  int written = snprintf(out, out_size, "%s.metallib", resolved);
  free(resolved);
  return (written > 0 && (size_t)written < out_size) ? 0 : -1;
}

static dispatch_data_t pcc_metal_embedded_library_data(void) {
  unsigned long size = 0;
  uint8_t *bytes = getsectiondata(
      &_mh_execute_header,
      "__PCCMETAL",
      "__metallib",
      &size);
  if (bytes == NULL || size == 0) {
    return nil;
  }
  return dispatch_data_create(
      bytes,
      (size_t)size,
      dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0),
      DISPATCH_DATA_DESTRUCTOR_DEFAULT);
}

int64_t pcc_metal_demo_add_f32(void) {
  @autoreleasepool {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (device == nil) {
      fprintf(stderr, "pcc metal: no Metal device available\n");
      return 3;
    }

    NSError *error = nil;
    const char *library_origin = "embedded metallib";
    dispatch_data_t embedded = pcc_metal_embedded_library_data();
    id<MTLLibrary> library = nil;
    if (embedded != nil) {
      library = [device newLibraryWithData:embedded error:&error];
      if (library == nil) {
        const char *msg = error ? [[error localizedDescription] UTF8String] : "unknown error";
        fprintf(stderr, "pcc metal: embedded metallib load failed: %s\n", msg);
      }
    }

    if (library == nil) {
      char lib_path[4096];
      if (pcc_metal_default_library_path(lib_path, sizeof(lib_path)) != 0) {
        fprintf(stderr, "pcc metal: could not derive sidecar metallib path\n");
        return 2;
      }
      error = nil;
      NSString *path = [NSString stringWithUTF8String:lib_path];
      NSURL *url = [NSURL fileURLWithPath:path];
      library = [device newLibraryWithURL:url error:&error];
      if (library == nil) {
        const char *msg = error ? [[error localizedDescription] UTF8String] : "unknown error";
        fprintf(stderr, "pcc metal: newLibraryWithURL failed: %s\n", msg);
        return 4;
      }
      library_origin = lib_path;
    }

    id<MTLFunction> function = [library newFunctionWithName:@"add"];
    if (function == nil) {
      fprintf(stderr, "pcc metal: kernel 'add' not found\n");
      return 5;
    }

    id<MTLComputePipelineState> pipeline =
        [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
      const char *msg = error ? [[error localizedDescription] UTF8String] : "unknown error";
      fprintf(stderr, "pcc metal: pipeline creation failed: %s\n", msg);
      return 6;
    }

    const uint32_t n = 8;
    const size_t bytes = n * sizeof(float);
    id<MTLBuffer> a = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
    id<MTLBuffer> b = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
    id<MTLBuffer> out = [device newBufferWithLength:bytes options:MTLResourceStorageModeShared];
    if (a == nil || b == nil || out == nil) {
      fprintf(stderr, "pcc metal: buffer allocation failed\n");
      return 7;
    }

    float *pa = (float *)[a contents];
    float *pb = (float *)[b contents];
    float *po = (float *)[out contents];
    for (uint32_t i = 0; i < n; ++i) {
      pa[i] = (float)i;
      pb[i] = 100.0f + (float)i;
      po[i] = -1.0f;
    }

    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> command_buffer = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:a offset:0 atIndex:0];
    [encoder setBuffer:b offset:0 atIndex:1];
    [encoder setBuffer:out offset:0 atIndex:2];
    [encoder setBytes:&n length:sizeof(n) atIndex:3];

    NSUInteger group_width = [pipeline maxTotalThreadsPerThreadgroup];
    if (group_width > n) {
      group_width = n;
    }
    [encoder dispatchThreads:MTLSizeMake(n, 1, 1)
        threadsPerThreadgroup:MTLSizeMake(group_width, 1, 1)];
    [encoder endEncoding];
    [command_buffer commit];
    [command_buffer waitUntilCompleted];

    if ([command_buffer error] != nil) {
      const char *msg = [[[command_buffer error] localizedDescription] UTF8String];
      fprintf(stderr, "pcc metal: command failed: %s\n", msg);
      return 8;
    }

    int ok = 1;
    for (uint32_t i = 0; i < n; ++i) {
      float expected = pa[i] + pb[i];
      if (po[i] != expected) {
        ok = 0;
      }
    }
    printf("%s Metal add kernel via %s\n", ok ? "OK" : "FAIL", library_origin);
    return ok ? 0 : 9;
  }
}
