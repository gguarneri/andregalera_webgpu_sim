"""
Example compute shader that does ... nothing but copy a value from one
buffer into another.
"""

import wgpu
import wgpu.backends.rs  # Select backend
from wgpu.utils import compute_with_buffers  # Convenience function

# %% Shader and data

shader_source = """
@group(0) @binding(0)
var<storage,read> data1: array<i32>;

@group(0) @binding(1)
var<storage,read_write> data2: array<i32>;

@group(0) @binding(2)
var<storage,read_write> k: array<i32>;

@stage(compute)
@workgroup_size(1)
fn incr_k() {
    k[0] += 1;
}

@stage(compute)
@workgroup_size(1)
fn main(@builtin(global_invocation_id) index: vec3<u32>) {
    let i: u32 = index.x;
    //data2[i] = data1[i] + data2[i] + k[0];
    data2[i] = data1[i] + data2[i] * k[0];
}
"""

# Create input data as a memoryview
n = 20
data = memoryview(bytearray(n * 4)).cast("i")
for i in range(n):
    data[i] = i

# %% The long version using the wgpu API

# Create device and shader object
device = wgpu.utils.get_default_device()
cshader = device.create_shader_module(code=shader_source)

# Create buffer objects, input buffer is mapped.
buffer1 = device.create_buffer_with_data(data=data, usage=wgpu.BufferUsage.STORAGE)
buffer2 = device.create_buffer(size=data.nbytes, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_SRC)
buffer3 = device.create_buffer(size=4, usage=wgpu.BufferUsage.STORAGE | wgpu.BufferUsage.COPY_SRC |
                                             wgpu.BufferUsage.COPY_DST)

# Setup layout and bindings
bl_0 = [
    {
        "binding": 0,
        "visibility": wgpu.ShaderStage.COMPUTE,
        "buffer": {
            "type": wgpu.BufferBindingType.read_only_storage,
        },
    },
# ]
# bl_1 = [
    {
        "binding": 1,
        "visibility": wgpu.ShaderStage.COMPUTE,
        "buffer": {
            "type": wgpu.BufferBindingType.storage,
        },
    },
# ]
# bl_2 = [
    {
        "binding": 2,
        "visibility": wgpu.ShaderStage.COMPUTE,
        "buffer": {
            "type": wgpu.BufferBindingType.storage,
        },
    },
]
bind_0 = [
    {
        "binding": 0,
        "resource": {"buffer": buffer1, "offset": 0, "size": buffer1.size},
    },
# ]
# bind_1 = [
    {
        "binding": 1,
        "resource": {"buffer": buffer2, "offset": 0, "size": buffer2.size},
    },
# ]
# bind_2 = [
    {
        "binding": 2,
        "resource": {"buffer": buffer3, "offset": 0, "size": buffer3.size},
    },
]

# Put everything together
bgl_0 = device.create_bind_group_layout(entries=bl_0)
# bgl_1 = device.create_bind_group_layout(entries=bl_1)
# bgl_2 = device.create_bind_group_layout(entries=bl_2)
# pipeline_layout = device.create_pipeline_layout(bind_group_layouts=[bgl_0, bgl_1, bgl_2])
pipeline_layout = device.create_pipeline_layout(bind_group_layouts=[bgl_0])
bg_0 = device.create_bind_group(layout=bgl_0, entries=bind_0)
# bg_1 = device.create_bind_group(layout=bgl_0, entries=bind_1)
# bg_2 = device.create_bind_group(layout=bgl_0, entries=bind_2)

# Create and run the pipeline
compute_pipeline = device.create_compute_pipeline(
    layout=pipeline_layout,
    compute={"module": cshader, "entry_point": "main"},
)
compute_incr_k = device.create_compute_pipeline(
    layout=pipeline_layout,
    compute={"module": cshader, "entry_point": "incr_k"},
)

command_encoder = device.create_command_encoder()
compute_pass = command_encoder.begin_compute_pass()
# compute_pass.set_pipeline(compute_pipeline)
compute_pass.set_bind_group(0, bg_0, [], 0, 999999)  # last 2 elements not used
# compute_pass.set_bind_group(1, bg_1, [], 0, 999999)  # last 2 elements not used
# compute_pass.set_bind_group(2, bg_2, [], 0, 999999)  # last 2 elements not used

for i in range(4):
    device.queue.write_buffer(buffer3, 0, i.to_bytes(4, 'little'))
    compute_pass.set_pipeline(compute_pipeline)
    compute_pass.dispatch_workgroups(n)  # x y z
    # compute_pass.end()

    # compute_pass.set_pipeline(compute_incr_k)
    # compute_pass.dispatch_workgroups(1)  # x y z

compute_pass.end()
device.queue.submit([command_encoder.finish()])

# Read result
out = device.queue.read_buffer(buffer2).cast("i")
k = device.queue.read_buffer(buffer3).cast("i").tolist()
result = out.tolist()
print(result)
assert result == list(range(20))
