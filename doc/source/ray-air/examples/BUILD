load("//bazel:python.bzl", "py_test_run_all_notebooks")

filegroup(
    name = "air_examples",
    srcs = glob(["*.ipynb"]),
    visibility = ["//doc:__subpackages__"]
)

# --------------------------------------------------------------------
# Test all doc/source/ray-air/examples notebooks.
# --------------------------------------------------------------------

py_test_run_all_notebooks(
    size = "large",
    include = ["*.ipynb"],
    exclude = ["huggingface_text_classification.ipynb"],
    data = ["//doc/source/ray-air/examples:air_examples"],
    tags = ["exclusive", "team:ml"],
)


# GPU Tests

py_test_run_all_notebooks(
    size = "large",
    include = ["huggingface_text_classification.ipynb"],
    exclude = [],
    data = ["//doc/source/ray-air/examples:air_examples"],
    tags = ["exclusive", "team:ml", "gpu"],
)