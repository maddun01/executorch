/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#import "ExecuTorchTensor.h"

NS_ASSUME_NONNULL_BEGIN

/**
 * Enum to define the dynamic type of a Value.
 * Values can be a subset, but must numerically match exactly those defined in
 * runtime/core/tag.h
 */
typedef NS_ENUM(uint32_t, ExecuTorchValueTag) {
  ExecuTorchValueTagNone,
  ExecuTorchValueTagTensor,
  ExecuTorchValueTagString,
  ExecuTorchValueTagDouble,
  ExecuTorchValueTagInteger,
  ExecuTorchValueTagBoolean,
  ExecuTorchValueTagBooleanList,
  ExecuTorchValueTagDoubleList,
  ExecuTorchValueTagIntegerList,
  ExecuTorchValueTagTensorList,
  ExecuTorchValueTagScalarList,
  ExecuTorchValueTagOptionalTensorList,
} NS_SWIFT_NAME(ValueTag);

/**
 * A dynamic value type used by ExecuTorch.
 *
 * ExecuTorchValue encapsulates a value that may be of various types such as
 * a tensor or a scalar. The value’s type is indicated by its tag.
 */
NS_SWIFT_NAME(Value)
__attribute__((deprecated("This API is experimental.")))
@interface ExecuTorchValue : NSObject

/**
 * The tag that indicates the dynamic type of the value.
 *
 * @return An ExecuTorchValueTag value.
 */
@property(nonatomic, readonly) ExecuTorchValueTag tag;

/**
 * Returns YES if the value is of type None.
 *
 * @return A BOOL indicating whether the value is None.
 */
@property(nonatomic, readonly) BOOL isNone;

@end

NS_ASSUME_NONNULL_END
