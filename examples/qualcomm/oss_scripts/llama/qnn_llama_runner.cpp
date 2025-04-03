/*
 * Copyright (c) Qualcomm Innovation Center, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

/**
 * @file
 *
 * This tool can run Llama2 110M, Llama3.2 1B / 3B(WIP) with Qualcomm AI Engine
 * Direct.
 *
 */

#include <executorch/backends/qualcomm/runtime/QnnExecuTorch.h>
#include <executorch/examples/qualcomm/oss_scripts/llama/runner/runner.h>
#include <executorch/runtime/platform/log.h>
#include <gflags/gflags.h>
#include <fstream>
#include <vector>

DEFINE_string(
    model_path,
    "kv_llama_qnn.pte",
    "Model serialized in flatbuffer format.");
DEFINE_string(
    output_path,
    "outputs.txt",
    "Executorch inference data output path.");
DEFINE_string(
    performance_output_path,
    "inference_speed.txt",
    "Records inference speed. For CI purpose.");
DEFINE_string(tokenizer_path, "tokenizer.bin", "Tokenizer stuff.");
DEFINE_string(prompt, "The answer to the ultimate question is", "Prompt.");
DEFINE_string(
    system_prompt,
    "",
    "Tells the model what kind of assistant it should be. For example, You are a helpful AI assistant for travel tips and recommendations. Default is None");
DEFINE_double(
    temperature,
    0.0f,
    "Temperature; Default is 0.0f. 0 = greedy argmax sampling (deterministic). Lower temperature = more deterministic");
DEFINE_int32(
    seq_len,
    128,
    "Total number of tokens to generate (prompt + output).");
DEFINE_int32(
    eval_mode,
    1,
    "0: TokenGenerator(kv) / 1: HybridMode (prefill+kv)");
DEFINE_double(logits_scale, 0.0, "Logits scale");
DEFINE_int32(logits_offset, 0, "Logits offset");
DEFINE_string(
    kv_updater,
    "How to update kv cache. Choose between SmartMask and ShiftPointer",
    "SmartMask");
DEFINE_int32(num_iters, 1, "total num of iterations to run.");

std::vector<std::string> CollectPrompts(int argc, char** argv) {
  // Collect all prompts from command line, example usage:
  // --prompt "prompt1" --prompt "prompt2" --prompt "prompt3"
  std::vector<std::string> prompts;
  for (int i = 1; i < argc; i++) {
    if (std::string(argv[i]) == "--prompt" && i + 1 < argc) {
      prompts.push_back(argv[i + 1]);
      i++; // Skip the next argument
    }
  }
  return prompts;
}

int main(int argc, char** argv) {
  std::vector<std::string> prompts = CollectPrompts(argc, argv);
  gflags::ParseCommandLineFlags(&argc, &argv, true);
  // create llama runner
  example::Runner runner(
      {FLAGS_model_path},
      FLAGS_tokenizer_path.c_str(),
      FLAGS_performance_output_path.c_str(),
      FLAGS_logits_scale,
      FLAGS_logits_offset,
      FLAGS_temperature,
      FLAGS_eval_mode,
      FLAGS_kv_updater,
      FLAGS_num_iters);
  std::vector<char> buf;
  buf.reserve(5 * FLAGS_seq_len); // assume each token is around 5 char
  std::ofstream fout(FLAGS_output_path.c_str());
  auto callback = [&](const std::string& piece) {
    for (const char c : piece) {
      buf.push_back(c);
    }
  };
  // generate tokens & store inference output
  for (int i = 0; i < FLAGS_num_iters; i++) {
    for (const auto& prompt : prompts) {
      runner.generate(
          FLAGS_seq_len, prompt.c_str(), FLAGS_system_prompt.c_str(), callback);
    }
  }
  fout.write(buf.data(), buf.size());
  fout.close();
  return 0;
}
