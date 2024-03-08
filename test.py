import numpy as np
import wgpu
import wgpu.backends.wgpu_native

data = np.array([1])

device = wgpu.utils.get_default_device()

cshader = None
with open('test_max.wgsl') as shader_file:
    cshader_str = shader_file.read()
    cshader = device.create_shader_module(code=cshader_str)

#TAMANHO MAXIMO DO BUFFER

max_bf = device.create_buffer(size=268435456, usage=wgpu.BufferUsage.STORAGE |
                                                        wgpu.BufferUsage.COPY_SRC)