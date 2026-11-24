// Python operations: a C++ stand-in factory in the library's OperationCatalog for each
// registered subclass of slideio_processor.Operation.
//
// GIL rules: no OperationFactory method touches Python; it answers from a snapshot of the
// class's declarations. A stand-in operation takes the GIL only around building the Python
// instance, apply, the array conversions and recording an error; it waits on its locks
// without the GIL.
#include "bindings.hpp"

#include <pybind11/numpy.h>
#include <pybind11/stl.h>

#include <slideio/processor/core/block_view.hpp>
#include <slideio/processor/core/data_type.hpp>
#include <slideio/processor/core/error.hpp>
#include <slideio/processor/pipeline/operation.hpp>
#include <slideio/processor/pipeline/operation_catalog.hpp>
#include <slideio/processor/pipeline/operation_parameters.hpp>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

namespace sp = slideio::processor;

enum class Kind { Bool, Int, Double, String, DoubleArray };

// A class's declarations, as Operation.__init_subclass__ checked them.
struct Snapshot {
    PyObject* cls = nullptr; // a strong reference, never released (decision 9)
    std::string version;
    std::string origin;
    bool labels = false;
    int32_t halo = 0;
    bool deterministic = true;
    int32_t maxConcurrency = 0; // 0: unlimited
    std::map<std::string, Kind> kinds;
    sp::DataType outputType = sp::DataType::Float32;
    int32_t outputPlanes = 1;
    int64_t workingSetBytesPerPixel = 0;
};
using SnapshotPtr = std::shared_ptr<const Snapshot>;

const std::map<std::string, Kind>& kindNames() {
    static const std::map<std::string, Kind> names{{"bool", Kind::Bool},
                                                   {"int", Kind::Int},
                                                   {"double", Kind::Double},
                                                   {"string", Kind::String},
                                                   {"doubleArray", Kind::DoubleArray}};
    return names;
}

const char* kindName(Kind kind) {
    switch (kind) {
    case Kind::Bool: return "a bool";
    case Kind::Int: return "an int";
    case Kind::Double: return "a float";
    case Kind::String: return "a string";
    case Kind::DoubleArray: return "an array of numbers";
    }
    return "?";
}

// The kind of the value a bag holds under a key it has. The bag has five setters, so a
// value that is none of the four scalars is a double array (possibly empty).
Kind kindOf(const sp::OperationParameters& parameters, const char* key) {
    bool b = false;
    int64_t i = 0;
    double d = 0;
    if (parameters.getBool(key, b)) return Kind::Bool;
    if (parameters.getInt(key, i)) return Kind::Int;
    if (parameters.getDouble(key, d)) return Kind::Double;
    if (parameters.getString(key, nullptr) != nullptr) return Kind::String;
    return Kind::DoubleArray;
}

sp::DataType dataTypeNamed(const std::string& name) {
    for (int32_t t = 0; t < static_cast<int32_t>(sp::DataType::Unknown); ++t) {
        const auto type = static_cast<sp::DataType>(t);
        if (name == sp::dataTypeName(type)) return type;
    }
    throw py::value_error("unknown data type " + name);
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

bool interpreterAlive() {
#if PY_VERSION_HEX >= 0x030D0000
    return Py_IsInitialized() && !Py_IsFinalizing();
#else
    return Py_IsInitialized() && !_Py_IsFinalizing();
#endif
}

// Decision 9. A worker that takes the GIL once finalization has begun blocks forever (or,
// before 3.14, exits the thread mid-call). The stand-ins therefore enter Python only through
// enterPython(), and an atexit handler, which runs before finalization begins, closes the
// gate: it sets `exiting` and waits, without the GIL, until every thread that entered has
// left. A thread raises the counter before reading the flag and the handler sets the flag
// before reading the counter (both sequentially consistent), so a thread either sees the
// flag and stays out or is waited for.
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

void leavePython() noexcept {
    ExitGate& gate = exitGate();
    if (gate.inPython.fetch_sub(1) == 1) {
        std::lock_guard<std::mutex> lock(gate.mutex);
        gate.left.notify_all();
    }
}

// True when the caller may take the GIL; it must then call leavePython() once it has
// released the GIL again.
bool enterPython() noexcept {
    ExitGate& gate = exitGate();
    gate.inPython.fetch_add(1);
    if (gate.exiting.load() || !interpreterAlive()) {
        leavePython();
        return false;
    }
    return true;
}

// The atexit handler. Called with the GIL held.
void closePython() {
    ExitGate& gate = exitGate();
    gate.exiting.store(true);
    py::gil_scoped_release release;
    std::unique_lock<std::mutex> lock(gate.mutex);
    gate.left.wait(lock, [&gate] { return gate.inPython.load() == 0; });
}

// Holds the GIL for a stand-in; throws when the interpreter is exiting.
class PythonScope {
public:
    explicit PythonScope(const std::string& name) {
        if (!enterPython()) throw std::runtime_error("operation '" + name + "': the Python interpreter is exiting");
        gil_.emplace();
    }
    ~PythonScope() {
        gil_.reset();
        leavePython();
    }
    PythonScope(const PythonScope&) = delete;
    PythonScope& operator=(const PythonScope&) = delete;

private:
    std::optional<py::gil_scoped_acquire> gil_;
};

// Decision 8: at most `limit` apply calls at once per registered name; 0 is no limit.
// Waited on without the GIL.
class ConcurrencyGate {
public:
    void enter(int32_t limit) {
        if (limit <= 0) return;
        std::unique_lock<std::mutex> lock(mutex_);
        free_.wait(lock, [&] { return active_ < limit; });
        ++active_;
    }
    void leave(int32_t limit) noexcept {
        if (limit <= 0) return;
        std::lock_guard<std::mutex> lock(mutex_);
        --active_;
        free_.notify_all();
    }

private:
    std::mutex mutex_;
    std::condition_variable free_;
    int32_t active_ = 0;
};

class GatePass {
public:
    GatePass(ConcurrencyGate& gate, int32_t limit) : gate_(gate), limit_(limit) { gate_.enter(limit_); }
    ~GatePass() { gate_.leave(limit_); }
    GatePass(const GatePass&) = delete;
    GatePass& operator=(const GatePass&) = delete;

private:
    ConcurrencyGate& gate_;
    const int32_t limit_;
};

// What apply's `context` sees of the tile; read-only. The token is the run's and valid only
// during apply: the stand-in clears it afterwards, and `cancelled` then reads true.
struct TileContext {
    int32_t level = 0;
    sp::Rect64 tileRect{};
    sp::Rect64 haloRect{};
    double levelScale = 1;
    double pixelSizeUm = 0;
    const sp::CancellationToken* token = nullptr; // read and cleared with the GIL held

    bool cancelled() const noexcept { return token == nullptr || token->isCancelled(); }
};

py::tuple rectTuple(const sp::Rect64& r) { return py::make_tuple(r.x, r.y, r.width, r.height); }

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

// The stage's values of the declared parameters, as a dict; "version" and "targetLayer"
// are left out. Call with the GIL held.
py::dict parameterValues(const sp::OperationParameters& parameters, const Snapshot& snapshot) {
    py::dict values;
    for (int32_t i = 0; i < parameters.count(); ++i) {
        const char* key = parameters.keyAt(i);
        const auto declared = snapshot.kinds.find(key);
        if (declared == snapshot.kinds.end()) continue;
        switch (kindOf(parameters, key)) {
        case Kind::Bool: {
            bool b = false;
            parameters.getBool(key, b);
            values[key] = b;
            break;
        }
        case Kind::Int: {
            int64_t v = 0;
            parameters.getInt(key, v);
            if (declared->second == Kind::Double) values[key] = static_cast<double>(v);
            else values[key] = v;
            break;
        }
        case Kind::Double: {
            double v = 0;
            parameters.getDouble(key, v);
            values[key] = v;
            break;
        }
        case Kind::String: values[key] = std::string(parameters.getString(key, "")); break;
        case Kind::DoubleArray: {
            std::vector<double> v(static_cast<size_t>(parameters.getDoubleArray(key, nullptr, 0)));
            parameters.getDoubleArray(key, v.data(), static_cast<int32_t>(v.size()));
            values[key] = v;
            break;
        }
        }
    }
    return values;
}

// The text of a Python exception, "<Type>: <str>". Never throws a Python error: a type name
// or str() that fails is replaced by a placeholder. Call with the GIL held.
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

// Decision 7: records the exception for SlideRun.run to chain as the cause. A record that
// cannot be made is dropped; the error's text still names the exception. Call with the GIL held.
void recordError(const std::string& name, const py::error_already_set& e) noexcept {
    try {
        py::module_::import("slideio_processor.operation").attr("_record_error")(name, e.value());
    } catch (...) {
    }
}

// The operation a run gets for a RASTER class. It builds the Python instance on its
// first tile, not when it is created: the library creates operations several times per run.
// Every declaration it reports comes from its own snapshot, so a run keeps the class it
// started with when the class is redefined meanwhile (decision 2).
class RasterStandIn final : public sp::Operation {
public:
    RasterStandIn(std::string name, SnapshotPtr snapshot, std::shared_ptr<ConcurrencyGate> gate,
                  const sp::OperationParameters& parameters)
        : name_(std::move(name)), snapshot_(std::move(snapshot)), gate_(std::move(gate)), parameters_(parameters) {}

    ~RasterStandIn() override {
        if (instance_.ptr() == nullptr) return;
        if (enterPython()) {
            {
                py::gil_scoped_acquire gil;
                instance_ = py::object();
            }
            leavePython();
        } else {
            instance_.release(); // the interpreter is exiting or gone: leak it (decision 9)
        }
    }

    const char* name() const override { return name_.c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
    sp::Halo halo() const override { return sp::Halo::uniform(snapshot_->halo); }
    sp::DataType outputType(sp::DataType) const override { return snapshot_->outputType; }
    const sp::OperationParameters& parameters() const override { return parameters_; }
    int32_t outputPlaneCount(int32_t) const override { return snapshot_->outputPlanes; }
    int64_t workingSetBytesPerLevelPixel() const override { return snapshot_->workingSetBytesPerPixel; }

    void apply(sp::OperationContext& context) override {
        ensureInstance();
        GatePass pass(*gate_, snapshot_->maxConcurrency);
        const sp::BlockView& in = context.input();
        const int64_t height = in.height();
        const int64_t width = in.width();
        const int32_t planes = in.planeCount();
        const sp::DataType inType = in.planeType(0);

        TileContext tileContext;
        tileContext.level = context.level();
        tileContext.tileRect = context.tileRectAtLevel();
        tileContext.haloRect = context.haloRectAtLevel();
        tileContext.levelScale = context.levelScale();
        tileContext.pixelSizeUm = context.pixelSizeMicrons();
        tileContext.token = &context.cancellation();
        const int64_t offsetX = tileContext.tileRect.x - tileContext.haloRect.x;
        const int64_t offsetY = tileContext.tileRect.y - tileContext.haloRect.y;

        std::string failure;
        {
            PythonScope python(name_);
            try {
                py::array tile(py::dtype(numpyName(inType)), std::vector<py::ssize_t>{height, width, planes});
                {
                    // Only this thread knows the new array: fill it without the GIL.
                    py::gil_scoped_release release;
                    fillTile(in, static_cast<char*>(tile.mutable_data()), height, width, planes, inType);
                }
                const py::object contextObject = py::cast(tileContext);
                py::object returned;
                try {
                    returned = instance_.attr("apply")(tile, contextObject);
                } catch (py::error_already_set& e) {
                    failure = describe(e);
                    recordError(name_, e);
                }
                contextObject.cast<TileContext&>().token = nullptr; // the token ends with this call
                if (failure.empty()) failure = copyOut(returned, height, width, offsetX, offsetY, context.output());
            } catch (py::error_already_set& e) {
                failure = "operation '" + name_ + "': " + describe(e);
            }
        }
        if (!failure.empty()) throw std::runtime_error(failure);
    }

private:
    enum State : int { Pending, Ready, Failed };

    // Decision 4: the mutex is taken without the GIL, then the GIL inside it. A failed
    // construction is kept: later tiles fail with the same text and __init__ runs once.
    void ensureInstance() {
        int state = state_.load(std::memory_order_acquire);
        if (state == Ready) return;
        if (state == Failed) throw std::runtime_error(initFailure_);
        std::lock_guard<std::mutex> lock(instanceMutex_);
        state = state_.load(std::memory_order_relaxed);
        if (state == Ready) return;
        if (state == Failed) throw std::runtime_error(initFailure_);
        std::string failure;
        {
            PythonScope python(name_);
            try {
                py::handle cls(snapshot_->cls);
                instance_ = py::module_::import("slideio_processor.operation")
                                .attr("_instantiate")(cls, parameterValues(parameters_, *snapshot_));
            } catch (py::error_already_set& e) {
                failure = describe(e);
                recordError(name_, e);
            }
        }
        if (!failure.empty()) {
            initFailure_ = failure;
            state_.store(Failed, std::memory_order_release);
            throw std::runtime_error(failure);
        }
        state_.store(Ready, std::memory_order_release);
    }

    // Interleaves the input planes into a C-contiguous (height, width, planes) buffer.
    static void fillTile(const sp::BlockView& in, char* dst, int64_t height, int64_t width, int32_t planes,
                         sp::DataType type) noexcept {
        const auto size = static_cast<size_t>(sp::dataTypeSizeBytes(type));
        const auto pixelStep = static_cast<py::ssize_t>(size) * planes;
        for (int64_t y = 0; y < height; ++y) {
            char* row = dst + y * width * pixelStep;
            for (int32_t p = 0; p < planes; ++p) {
                const char* src = static_cast<const char*>(in.planeData(p)) + y * in.planeRowStrideBytes(p);
                copyElements(size, row + p * size, pixelStep, src, static_cast<py::ssize_t>(size), width);
            }
        }
    }

    // Decision 6: checks what apply returned, then copies its tile region, which starts at
    // (offsetX, offsetY), into the output. Returns a failure text, or "". Call with the GIL held.
    std::string copyOut(const py::object& returned, int64_t height, int64_t width, int64_t offsetX, int64_t offsetY,
                        sp::BlockView& out) const {
        const std::string label = "operation '" + name_ + "': apply returned ";
        if (!py::isinstance<py::array>(returned)) {
            return label + Py_TYPE(returned.ptr())->tp_name + ", expected a numpy.ndarray";
        }
        const auto array = py::reinterpret_borrow<py::array>(returned);
        const int32_t planes = snapshot_->outputPlanes;
        const char* expected = numpyName(snapshot_->outputType);
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
            return "operation '" + name_ + "': the output does not fit the returned array";
        }
        const auto size = static_cast<size_t>(sp::dataTypeSizeBytes(snapshot_->outputType));
        const auto* base = static_cast<const char*>(array.data());
        const py::ssize_t planeStride = array.ndim() == 3 ? array.strides(2) : 0;
        for (int32_t p = 0; p < planes; ++p) {
            for (int64_t y = 0; y < out.height(); ++y) {
                char* dst = static_cast<char*>(out.planeData(p)) + y * out.planeRowStrideBytes(p);
                const char* src =
                    base + (y + offsetY) * array.strides(0) + offsetX * array.strides(1) + p * planeStride;
                copyElements(size, dst, static_cast<py::ssize_t>(size), src, array.strides(1), out.width());
            }
        }
        return {};
    }

    const std::string name_;
    const SnapshotPtr snapshot_;
    const std::shared_ptr<ConcurrencyGate> gate_; // the registered name's (decision 8)
    const sp::OperationParameters parameters_;
    std::mutex instanceMutex_;
    std::atomic<int> state_{Pending};
    std::string initFailure_; // written once, before state_ becomes Failed
    py::object instance_;     // set once under instanceMutex_ and the GIL
};

// The catalog's factory for one registered name. A redefinition swaps its snapshot.
class PythonOperationFactory final : public sp::OperationFactory {
public:
    PythonOperationFactory(std::string name, SnapshotPtr snapshot) : name_(std::move(name)) { replace(snapshot); }

    void replace(SnapshotPtr snapshot) {
        std::lock_guard<std::mutex> lock(mutex_);
        // Every snapshot is kept, so a pointer origin() returned stays valid.
        history_.push_back(snapshot);
        current_ = std::move(snapshot);
    }

    SnapshotPtr snapshot() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return current_;
    }

    sp::OperationPtr create(const sp::OperationParameters& parameters) const override {
        SnapshotPtr s = snapshot();
        check(parameters, *s);
        if (s->labels) {
            // Task 4 adds the LABELS stand-in.
            throw sp::InvalidArgumentError(("operation '" + name_ + "': LABELS operations cannot run yet").c_str());
        }
        return std::make_shared<RasterStandIn>(name_, std::move(s), gate_, parameters);
    }

    const char* name() const override { return name_.c_str(); }
    sp::ChannelArity arity() const override { return sp::ChannelArity::Joint; }
    bool requiresUniformTypes() const override { return true; }
    sp::DataTypeSet supportedTypes() const override { return sp::DataTypeSet::all(); }
    // These answer from the current snapshot: what the next run gets. A stand-in already
    // created answers from its own, and the library takes the stand-in's word (decision 2).
    sp::Halo halo() const override { return sp::Halo::uniform(snapshot()->halo); }
    const char* correctnessReference() const override { return "a Python class; see its origin"; }
    const char* complexityNote() const override { return ""; }
    int64_t workingSetBytesPerLevelPixel() const override { return snapshot()->workingSetBytesPerPixel; }
    bool producesObjects() const override { return snapshot()->labels; }
    bool isDeterministic() const override { return snapshot()->deterministic; }
    const char* origin() const override { return snapshot()->origin.c_str(); }
    int32_t outputPlaneCount(int32_t) const override { return snapshot()->outputPlanes; }

private:
    [[noreturn]] void refuse(const std::string& text) const {
        sp::ErrorContext context;
        context.operationName = name_.c_str();
        throw sp::InvalidArgumentError(("operation '" + name_ + "': " + text).c_str(), context);
    }

    // Global Constraints and decision 3. Messages name keys, never values (the version
    // excepted).
    void check(const sp::OperationParameters& parameters, const Snapshot& s) const {
        bool hasVersion = false;
        for (int32_t i = 0; i < parameters.count(); ++i) {
            const std::string key = parameters.keyAt(i);
            const Kind kind = kindOf(parameters, key.c_str());
            if (key == "version") {
                if (kind != Kind::String) refuse("the stage parameter 'version' must be a string");
                const std::string version = parameters.getString("version", "");
                if (version != s.version) {
                    refuse("the stage carries version '" + version + "', the class has version '" + s.version + "'");
                }
                hasVersion = true;
                continue;
            }
            if (key == "targetLayer" && s.labels) {
                if (kind != Kind::String) refuse("the stage parameter 'targetLayer' must be a string");
                continue;
            }
            const auto declared = s.kinds.find(key);
            if (declared == s.kinds.end()) refuse("unknown stage parameter '" + key + "'");
            if (kind != declared->second && !(declared->second == Kind::Double && kind == Kind::Int)) {
                refuse("the stage parameter '" + key + "' must be " + kindName(declared->second));
            }
        }
        if (!hasVersion) refuse("the stage parameters carry no 'version'; the class has version '" + s.version + "'");
    }

    const std::string name_;
    const std::shared_ptr<ConcurrencyGate> gate_ = std::make_shared<ConcurrencyGate>(); // per name
    mutable std::mutex mutex_;
    SnapshotPtr current_;
    std::vector<SnapshotPtr> history_;
};

// The registered Python operations. Never destroyed: the catalog, which shares the
// factories, is destroyed after Python has finalized (decision 9).
std::mutex registryMutex;
std::map<std::string, std::shared_ptr<PythonOperationFactory>>& registry() {
    static auto* factories = new std::map<std::string, std::shared_ptr<PythonOperationFactory>>();
    return *factories;
}

SnapshotPtr toSnapshot(const py::handle& cls, const py::dict& d) {
    auto s = std::make_shared<Snapshot>();
    s->version = d["version"].cast<std::string>();
    s->origin = d["origin"].cast<std::string>();
    s->labels = d["output"].cast<std::string>() == "labels";
    s->halo = d["halo"].cast<int32_t>();
    s->deterministic = d["deterministic"].cast<bool>();
    s->maxConcurrency = d["max_concurrency"].cast<int32_t>();
    for (const auto& item : d["parameter_kinds"].cast<py::dict>()) {
        s->kinds[item.first.cast<std::string>()] = kindNames().at(item.second.cast<std::string>());
    }
    s->outputType = dataTypeNamed(d["output_type"].cast<std::string>());
    s->outputPlanes = d["output_planes"].cast<int32_t>();
    s->workingSetBytesPerPixel = d["working_set_bytes_per_pixel"].cast<int64_t>();
    s->cls = cls.inc_ref().ptr();
    return s;
}

void registerPythonOperation(const py::handle& cls, const py::dict& snapshot) {
    const std::string name = snapshot["name"].cast<std::string>();
    std::lock_guard<std::mutex> lock(registryMutex);
    auto& factories = registry();
    const auto found = factories.find(name);
    if (found != factories.end()) {
        const bool labels = snapshot["output"].cast<std::string>() == "labels";
        if (found->second->snapshot()->labels != labels) {
            throw sp::InvalidArgumentError(("operation '" + name + "': a redefinition cannot change output from " +
                                            (labels ? "RASTER to LABELS" : "LABELS to RASTER"))
                                               .c_str());
        }
        found->second->replace(toSnapshot(cls, snapshot));
        return;
    }
    sp::OperationCatalog& catalog = sp::OperationCatalog::instance();
    if (catalog.isRegistered(name.c_str())) {
        throw sp::InvalidArgumentError(
            ("operation '" + name + "': the name belongs to a built-in operation").c_str());
    }
    auto factory = std::make_shared<PythonOperationFactory>(name, toSnapshot(cls, snapshot));
    catalog.registerFactory(factory);
    factories.emplace(name, std::move(factory));
}

} // namespace

void bindOperations(py::module_& m) {
    py::class_<TileContext>(m, "TileContext",
                            "What apply's context sees of the tile: read-only, and valid only during apply.")
        .def_readonly("level", &TileContext::level, "The analysis level, 0 for full resolution.")
        .def_property_readonly(
            "tile_rect", [](const TileContext& c) { return rectTuple(c.tileRect); },
            "(x, y, width, height) of the tile at the analysis level.")
        .def_property_readonly(
            "halo_rect", [](const TileContext& c) { return rectTuple(c.haloRect); },
            "(x, y, width, height) of the tile grown by the halo, at the analysis level; not clipped to the level.")
        .def_readonly("level_scale", &TileContext::levelScale, "The factor from the analysis level to level 0.")
        .def_readonly("pixel_size_um", &TileContext::pixelSizeUm,
                      "The level-0 pixel size in micrometres; 0 when unknown.")
        .def_property_readonly("cancelled", &TileContext::cancelled,
                               "True once the run is cancelled; always True after apply has returned.");
    // Python runs atexit handlers after joining its non-daemon threads and before it begins
    // finalizing: from then on the stand-ins stay out of Python (decision 9).
    py::module_::import("atexit").attr("register")(py::cpp_function(&closePython));
    m.def("_register_python_operation", &registerPythonOperation, py::arg("cls"), py::arg("snapshot"),
          "Registers, or redefines, a Python operation from its class and snapshot.");
    m.def(
        "_catalog_names",
        []() {
            const sp::OperationCatalog& catalog = sp::OperationCatalog::instance();
            std::vector<std::string> names;
            for (int32_t i = 0; i < catalog.count(); ++i) names.emplace_back(catalog.nameAt(i));
            return names;
        },
        "The catalog's operation names, in its order.");
    m.def(
        "_is_python_operation",
        [](const std::string& name) {
            std::lock_guard<std::mutex> lock(registryMutex);
            return registry().count(name) != 0;
        },
        py::arg("name"), "True when a Python class registered the name.");
}
