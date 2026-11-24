#include "python_runtime.hpp"

#include <atomic>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace slideio_processor_python {

namespace {

bool interpreterAlive() {
#if PY_VERSION_HEX >= 0x030D0000
    return Py_IsInitialized() && !Py_IsFinalizing();
#else
    return Py_IsInitialized() && !_Py_IsFinalizing();
#endif
}

// A thread raises the counter before reading the flag and the handler sets the flag before
// reading the counter (both sequentially consistent), so a thread either sees the flag and
// stays out or is waited for.
struct ExitGate {
    std::atomic<bool> exiting{false};
    std::atomic<int64_t> inPython{0};
    std::mutex mutex;
    std::condition_variable left;
};

// Never destroyed: a worker may still pass through it during static destruction.
ExitGate& exitGate() {
    static auto* gate = new ExitGate();
    return *gate;
}

// Copies `count` elements of `Size` bytes between strided runs.
template <size_t Size>
void copyRun(char* dst, py::ssize_t dstStep, const char* src, py::ssize_t srcStep, int64_t count) noexcept {
    for (int64_t i = 0; i < count; ++i) std::memcpy(dst + i * dstStep, src + i * srcStep, Size);
}

void copyElements(size_t size, char* dst, py::ssize_t dstStep, const char* src, py::ssize_t srcStep,
                  int64_t count) noexcept {
    if (static_cast<py::ssize_t>(size) == dstStep && dstStep == srcStep) {
        std::memcpy(dst, src, static_cast<size_t>(count) * size);
        return;
    }
    switch (size) {
    case 1: copyRun<1>(dst, dstStep, src, srcStep, count); break;
    case 2: copyRun<2>(dst, dstStep, src, srcStep, count); break;
    case 4: copyRun<4>(dst, dstStep, src, srcStep, count); break;
    case 8: copyRun<8>(dst, dstStep, src, srcStep, count); break;
    default:
        for (int64_t i = 0; i < count; ++i) std::memcpy(dst + i * dstStep, src + i * srcStep, size);
        break;
    }
}

std::string shapeText(const std::vector<py::ssize_t>& shape) {
    std::string text = "(";
    for (size_t i = 0; i < shape.size(); ++i) {
        if (i > 0) text += ", ";
        text += std::to_string(shape[i]);
    }
    return text + (shape.size() == 1 ? ",)" : ")");
}

} // namespace

void leavePython() noexcept {
    ExitGate& gate = exitGate();
    if (gate.inPython.fetch_sub(1) == 1) {
        std::lock_guard<std::mutex> lock(gate.mutex);
        gate.left.notify_all();
    }
}

bool enterPython() noexcept {
    ExitGate& gate = exitGate();
    gate.inPython.fetch_add(1);
    if (gate.exiting.load() || !interpreterAlive()) {
        leavePython();
        return false;
    }
    return true;
}

void closePython() {
    ExitGate& gate = exitGate();
    gate.exiting.store(true);
    py::gil_scoped_release release;
    std::unique_lock<std::mutex> lock(gate.mutex);
    gate.left.wait(lock, [&gate] { return gate.inPython.load() == 0; });
}

PythonScope::PythonScope(const std::string& name) {
    if (!enterPython()) throw std::runtime_error("operation '" + name + "': the Python interpreter is exiting");
    gil_.emplace();
}

PythonScope::~PythonScope() {
    gil_.reset();
    leavePython();
}

void ConcurrencyGate::enter(int32_t limit) {
    if (limit <= 0) return;
    std::unique_lock<std::mutex> lock(mutex_);
    free_.wait(lock, [&] { return active_ < limit; });
    ++active_;
}

void ConcurrencyGate::leave(int32_t limit) noexcept {
    if (limit <= 0) return;
    std::lock_guard<std::mutex> lock(mutex_);
    --active_;
    free_.notify_all();
}

const char* numpyName(sp::DataType type) {
    switch (type) {
    case sp::DataType::Byte: return "uint8";
    case sp::DataType::Int8: return "int8";
    case sp::DataType::UInt16: return "uint16";
    case sp::DataType::Int16: return "int16";
    case sp::DataType::UInt32: return "uint32";
    case sp::DataType::Int32: return "int32";
    case sp::DataType::UInt64: return "uint64";
    case sp::DataType::Int64: return "int64";
    case sp::DataType::Float16: return "float16";
    case sp::DataType::Float32: return "float32";
    case sp::DataType::Float64: return "float64";
    default: break;
    }
    throw std::runtime_error("no NumPy dtype for this data type");
}

void fillTile(const sp::BlockView& in, char* dst) noexcept {
    const int64_t height = in.height();
    const int64_t width = in.width();
    const int32_t planes = in.planeCount();
    const auto size = static_cast<size_t>(sp::dataTypeSizeBytes(in.planeType(0)));
    const auto pixelStep = static_cast<py::ssize_t>(size) * planes;
    for (int64_t y = 0; y < height; ++y) {
        char* row = dst + y * width * pixelStep;
        for (int32_t p = 0; p < planes; ++p) {
            const char* src = static_cast<const char*>(in.planeData(p)) + y * in.planeRowStrideBytes(p);
            copyElements(size, row + p * size, pixelStep, src, static_cast<py::ssize_t>(size), width);
        }
    }
}

std::string copyResult(const std::string& name, const py::object& returned, sp::DataType type, int32_t planes,
                       int64_t height, int64_t width, int64_t offsetX, int64_t offsetY, sp::BlockView& out) {
    const std::string label = "operation '" + name + "': apply returned ";
    if (!py::isinstance<py::array>(returned)) {
        return label + Py_TYPE(returned.ptr())->tp_name + ", expected a numpy.ndarray";
    }
    const auto array = py::reinterpret_borrow<py::array>(returned);
    const char* expected = numpyName(type);
    if (!array.dtype().equal(py::dtype(expected))) {
        return label + "dtype " + std::string(py::str(array.dtype())) + ", expected " + expected;
    }
    const bool shapeOk = array.ndim() == 3 ? array.shape(2) == planes : array.ndim() == 2 && planes == 1;
    if (!shapeOk || array.shape(0) != height || array.shape(1) != width) {
        const std::vector<py::ssize_t> actual(array.shape(), array.shape() + array.ndim());
        std::string wanted = planes == 1 ? shapeText({height, width}) + " or " : std::string();
        wanted += shapeText({height, width, planes});
        return label + "shape " + shapeText(actual) + ", expected " + wanted;
    }
    if (out.planeCount() != planes || offsetX < 0 || offsetY < 0 || offsetX + out.width() > width ||
        offsetY + out.height() > height) {
        return "operation '" + name + "': the output does not fit the returned array";
    }
    const auto size = static_cast<size_t>(sp::dataTypeSizeBytes(type));
    const auto* base = static_cast<const char*>(array.data());
    const py::ssize_t planeStride = array.ndim() == 3 ? array.strides(2) : 0;
    for (int32_t p = 0; p < planes; ++p) {
        for (int64_t y = 0; y < out.height(); ++y) {
            char* dst = static_cast<char*>(out.planeData(p)) + y * out.planeRowStrideBytes(p);
            const char* src = base + (y + offsetY) * array.strides(0) + offsetX * array.strides(1) + p * planeStride;
            copyElements(size, dst, static_cast<py::ssize_t>(size), src, array.strides(1), out.width());
        }
    }
    return {};
}

std::string describe(const py::error_already_set& e) {
    std::string text = Py_TYPE(e.value().ptr())->tp_name;
    try {
        text = py::str(py::type::of(e.value()).attr("__name__"));
    } catch (const std::exception&) {
    }
    std::string message;
    try {
        message = py::str(e.value());
    } catch (const std::exception&) {
        message = "<str() of the exception failed>";
    }
    if (!message.empty()) text += ": " + message;
    return text;
}

void recordError(const std::string& name, const py::error_already_set& e) noexcept {
    try {
        py::module_::import("slideio_processor.operation").attr("_record_error")(name, e.value());
    } catch (...) {
    }
}

} // namespace slideio_processor_python
