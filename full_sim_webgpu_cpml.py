import math
import wgpu.backends.rs  # Select backend
from wgpu.utils import compute_with_buffers  # Convenience function
import numpy as np
from sklearn.metrics import mean_squared_error
import matplotlib.pyplot as plt
from time import time
from datetime import datetime
from PyQt5.QtWidgets import *
import pyqtgraph as pg
from pyqtgraph.widgets.RawImageWidget import RawImageGLWidget


# ==========================================================
# Esse arquivo contém as simulações realizadas dentro da GPU.
# ==========================================================

# Código para visualização da janela de simulação
# Image View class
class ImageView(pg.ImageView):
    # constructor which inherit original
    # ImageView
    def __init__(self, *args, **kwargs):
        pg.ImageView.__init__(self, *args, **kwargs)


# RawImageWidget class
class RawImageWidget(pg.widgets.RawImageWidget.RawImageGLWidget):
    # constructor which inherit original
    # RawImageWidget
    def __init__(self):
        pg.widgets.RawImageWidget.RawImageGLWidget.__init__(self)


# Window class
class Window(QMainWindow):
    def __init__(self):
        super().__init__()

        # setting title
        self.setWindowTitle(f"{ny}x{nx} Grid x {nstep} iterations - dx = {dx} m x dy = {dy} m x dt = {dt} s")

        # setting geometry
        # self.setGeometry(200, 50, 1600, 800)
        self.setGeometry(200, 50, 500, 500)

        # setting animation
        self.isAnimated()

        # setting image
        self.image = np.random.normal(size=(500, 500))

        # showing all the widgets
        self.show()

        # creating a widget object
        self.widget = QWidget()

        # setting configuration options
        pg.setConfigOptions(antialias=True)

        # creating image view view object
        self.imv = RawImageWidget()

        # setting image to image view
        self.imv.setImage(self.image, levels=[-0.1, 0.1])

        # Creating a grid layout
        self.layout = QGridLayout()

        # setting this layout to the widget
        self.widget.setLayout(self.layout)

        # plot window goes on right side, spanning 3 rows
        self.layout.addWidget(self.imv, 0, 0, 4, 1)

        # setting this widget as central widget of the main window
        self.setCentralWidget(self.widget)


# Parametros dos ensaios
flt32 = np.float32
n_iter_gpu = 1
n_iter_cpu = 1
do_sim_gpu = True
do_sim_cpu = True
do_comp_fig_cpu_gpu = True
use_refletors = False
plot_results = True
show_results = True
save_results = True
gpu_type = "NVIDIA"

# Parametros da simulacao
nx = 801  # colunas
ny = 801  # linhas

# Tamanho do grid (aparentemente em metros)
dx = 1.5
dy = dx

# Espessura da PML in pixels
npoints_pml = 10

# Velocidade do som e densidade do meio
cp_unrelaxed = 2000.0  # [m/s]
density = 2000.0  # [kg / m ** 3]
rho = (density * np.ones((ny, nx))).astype(flt32)  # Campo de densidade do meio de propagacao

# Interpolacao da densidade nos pontos intermediarios do grid (staggered grid)
rho_half_x = np.zeros((ny, nx), dtype=flt32)
rho_half_y = np.zeros((ny, nx), dtype=flt32)
rho_half_x[:, :-1] = 0.5 * (rho[:, 1:] + rho[:, :-1])
rho_half_x[:, nx - 1] = rho_half_x[:, nx - 2]
rho_half_y[:-1, :] = 0.5 * (rho[1:, :] + rho[:-1, :])
rho_half_y[ny - 1, :] = rho_half_y[ny - 2, :]

# Calculo da rigidez (stiffness - Lame parameter)
kappa_unrelaxed = (density * cp_unrelaxed ** 2 * np.ones((ny, nx))).astype(flt32)

# Numero total de passos de tempo
nstep = 1500

# Passo de tempo em segundos
dt = 5.2e-4

# Parametros da fonte
f0 = 35.0  # frequencia
t0 = 1.20 / f0  # delay
factor = 1.0
a = math.pi ** 2 * f0 ** 2
t = np.arange(nstep) * dt

# Funcao de Ricker (segunda derivada de uma gaussiana)
source_term = (factor * (1.0 - 2.0 * a * (t - t0) ** 2) * np.exp(-a * (t - t0) ** 2)).astype(flt32)

# Posicao da fonte
xsource = 600.0
ysource = 600.0
isource = int(xsource / dx) + 1
jsource = int(ysource / dy) + 1
kronecker_source = np.zeros((ny, nx), dtype=flt32)  # Posicoes das fontes
kronecker_source[jsource, isource] = np.float32(1.0)

# Receptores
xdeb = 561.0  # em unidade de distancia
ydeb = 561.0  # em unidade de distancia
sens_x = int(xdeb / dx) + 1
sens_y = int(ydeb / dy) + 1
sensor = np.zeros(nstep, dtype=flt32)  # buffer para sinal do sensor

# Escolha do valor de wsx
wsx = 1
for n in range(15, 0, -1):
    if (ny % n) == 0:
        wsx = n  # workgroup x size
        break

# Escolha do valor de wsy
wsy = 1
for n in range(15, 0, -1):
    if (nx % n) == 0:
        wsy = n  # workgroup x size
        break

# Campo de pressao
p_2 = np.zeros((ny, nx), dtype=flt32)  # pressao futura
p_1 = np.zeros((ny, nx), dtype=flt32)  # pressao atual
p_0 = np.zeros((ny, nx), dtype=flt32)  # pressao passada

# Campos auxiliares para calculo das derivadas espaciais com CPML
mdp_x = np.zeros((ny, nx), dtype=flt32)
dp_x = np.zeros((ny, nx), dtype=flt32)
dmdp_x = np.zeros((ny, nx), dtype=flt32)

mdp_y = np.zeros((ny, nx), dtype=flt32)
dp_y = np.zeros((ny, nx), dtype=flt32)
dmdp_y = np.zeros((ny, nx), dtype=flt32)

# Campos de velocidade
v_x = np.zeros((ny, nx), dtype=flt32)
v_y = np.zeros((ny, nx), dtype=flt32)

# Valor da potencia para calcular "d0"
NPOWER = 2.0
if NPOWER < 1:
    raise ValueError('NPOWER deve ser maior que 1')

# from Stephen Gedney's unpublished class notes for class EE699, lecture 8, slide 8-11
K_MAX_PML = 1.0
ALPHA_MAX_PML = 2.0 * math.pi * (f0 / 2)  # from Festa and Vilotte

# Constantes
HUGEVAL = 1.0e30  # Valor enorme para o maximo da pressao
STABILITY_THRESHOLD = 1.0e25  # Limite para considerar que a simulacao esta instavel

# Inicializacao dos parametros da PML (definicao dos perfis de absorcao na regiao da PML)
thickness_pml_x = npoints_pml * dx
thickness_pml_y = npoints_pml * dy

# Coeficiente de reflexao (INRIA report section 6.1) http://hal.inria.fr/docs/00/07/32/19/PDF/RR-3471.pdf
rcoef = 0.001

# Calculo de d0 do relatorio da INRIA section 6.1 http://hal.inria.fr/docs/00/07/32/19/PDF/RR-3471.pdf
d0_x = -(NPOWER + 1) * cp_unrelaxed * math.log(rcoef) / (2.0 * thickness_pml_x)
d0_y = -(NPOWER + 1) * cp_unrelaxed * math.log(rcoef) / (2.0 * thickness_pml_y)

# Amortecimento na direcao "x" (horizontal)
# Origem da PML (posicao das bordas direita e esquerda menos a espessura, em unidades de distancia)
x_orig_left = thickness_pml_x
x_orig_right = (nx - 1) * dx - thickness_pml_x

# Perfil de amortecimento na direcao "x" dentro do grid de pressao
i = np.arange(nx)
xval = dx * i
xval_pml_left = x_orig_left - xval
xval_pml_right = xval - x_orig_right
x_pml_mask_left = np.where(xval_pml_left < 0.0, False, True)
x_pml_mask_right = np.where(xval_pml_right < 0.0, False, True)
x_mask = np.logical_or(x_pml_mask_left, x_pml_mask_right)
x_pml = np.zeros(nx)
x_pml[x_pml_mask_left] = xval_pml_left[x_pml_mask_left]
x_pml[x_pml_mask_right] = xval_pml_right[x_pml_mask_right]
x_norm = x_pml / thickness_pml_x
d_x = (d0_x * x_norm ** NPOWER).astype(flt32)
k_x = (1.0 + (K_MAX_PML - 1.0) * x_norm ** NPOWER).astype(flt32)
alpha_x = (ALPHA_MAX_PML * (1.0 - np.where(x_mask, x_norm, 1.0))).astype(flt32)
b_x = np.exp(-(d_x / k_x + alpha_x) * dt).astype(flt32)
i = np.where(d_x > 1e-6)
a_x = np.zeros(nx, dtype=flt32)
a_x[i] = d_x[i] * (b_x[i] - 1.0) / (k_x[i] * (d_x[i] + k_x[i] * alpha_x[i]))

# Perfil de amortecimento na direcao "x" dentro do meio grid de pressao (staggered grid)
xval_pml_left = x_orig_left - (xval + dx / 2.0)
xval_pml_right = (xval + dx / 2.0) - x_orig_right
x_pml_mask_left = np.where(xval_pml_left < 0.0, False, True)
x_pml_mask_right = np.where(xval_pml_right < 0.0, False, True)
x_mask_half = np.logical_or(x_pml_mask_left, x_pml_mask_right)
x_pml = np.zeros(nx)
x_pml[x_pml_mask_left] = xval_pml_left[x_pml_mask_left]
x_pml[x_pml_mask_right] = xval_pml_right[x_pml_mask_right]
x_norm = x_pml / thickness_pml_x
d_x_half = (d0_x * x_norm ** NPOWER).astype(flt32)
k_x_half = (1.0 + (K_MAX_PML - 1.0) * x_norm ** NPOWER).astype(flt32)
alpha_x_half = (ALPHA_MAX_PML * (1.0 - np.where(x_mask_half, x_norm, 1.0))).astype(flt32)
b_x_half = np.exp(-(d_x_half / k_x_half + alpha_x_half) * dt)
a_x_half = np.zeros(nx, dtype=flt32)
i = np.where(d_x_half > 1e-6)
a_x_half[i] = d_x_half[i] * (b_x_half[i] - 1.0) / (k_x_half[i] * (d_x_half[i] + k_x_half[i] * alpha_x_half[i]))

# Amortecimento na direcao "y" (vertical)
# Origem da PML (posicao das bordas superior e inferior menos a espessura, em unidades de distancia)
y_orig_top = thickness_pml_y
y_orig_bottom = (ny - 1) * dy - thickness_pml_y

# Perfil de amortecimento na direcao "y" dentro do grid de pressao
j = np.arange(ny)
yval = dy * j
y_pml_top = y_orig_top - yval
y_pml_bottom = yval - y_orig_bottom
y_pml_mask_top = np.where(y_pml_top < 0.0, False, True)
y_pml_mask_bottom = np.where(y_pml_bottom < 0.0, False, True)
y_mask = np.logical_or(y_pml_mask_top, y_pml_mask_bottom)
y_pml = np.zeros(ny)
y_pml[y_pml_mask_top] = y_pml_top[y_pml_mask_top]
y_pml[y_pml_mask_bottom] = y_pml_bottom[y_pml_mask_bottom]
y_norm = y_pml / thickness_pml_y
d_y = (d0_y * y_norm ** NPOWER).astype(flt32)
k_y = (1.0 + (K_MAX_PML - 1.0) * y_norm ** NPOWER).astype(flt32)
alpha_y = (ALPHA_MAX_PML * (1.0 - np.where(y_mask, y_norm, 1.0))).astype(flt32)
b_y = np.exp(-(d_y / k_y + alpha_y) * dt).astype(flt32)
a_y = np.zeros(ny, dtype=flt32)
j = np.where(d_y > 1e-6)
a_y[j] = d_y[j] * (b_y[j] - 1.0) / (k_y[j] * (d_y[j] + k_y[j] * alpha_y[j]))

# Perfil de amortecimento na direcao "x" dentro do meio grid de pressao (staggered grid)
y_pml_top = y_orig_top - (yval + dy / 2.0)
y_pml_bottom = (yval + dy / 2.0) - y_orig_bottom
y_pml_mask_top = np.where(y_pml_top < 0.0, False, True)
y_pml_mask_bottom = np.where(y_pml_bottom < 0.0, False, True)
y_mask_half = np.logical_or(y_pml_mask_top, y_pml_mask_bottom)
y_pml = np.zeros(ny)
y_pml[y_pml_mask_top] = y_pml_top[y_pml_mask_top]
y_pml[y_pml_mask_bottom] = y_pml_bottom[y_pml_mask_bottom]
y_norm = y_pml / thickness_pml_y
d_y_half = (d0_y * y_norm ** NPOWER).astype(flt32)
k_y_half = (1.0 + (K_MAX_PML - 1.0) * y_norm ** NPOWER).astype(flt32)
alpha_y_half = (ALPHA_MAX_PML * (1.0 - np.where(y_mask_half, y_norm, 1.0))).astype(flt32)
b_y_half = np.exp(-(d_y_half / k_y_half + alpha_y_half) * dt).astype(flt32)
a_y_half = np.zeros(ny, dtype=flt32)
j = np.where(d_y_half > 1e-6)
a_y_half[j] = d_y_half[j] * (b_y_half[j] - 1.0) / (k_y_half[j] * (d_y_half[j] + k_y_half[j] * alpha_y_half[j]))


def sim_cpu():
    global p_2, p_1, p_0, mdp_x, mdp_y, dp_x, dp_y, dmdp_x, dmdp_y, v_x, v_y

    # Definicao das matrizes auxiliares
    vdp_x = np.zeros((ny, nx), dtype=flt32)
    vdp_y = np.zeros((ny, nx), dtype=flt32)
    vdp_xx = np.zeros((ny, nx), dtype=flt32)
    vdp_yy = np.zeros((ny, nx), dtype=flt32)

    # Acerta as dimensões dos vetores no sentido "y"
    a_y_t = a_y[:, np.newaxis]
    a_y_half_t = a_y_half[:, np.newaxis]
    b_y_t = b_y[:, np.newaxis]
    b_y_half_t = b_y_half[:, np.newaxis]
    k_y_t = k_y[:, np.newaxis]
    k_y_half_t = k_y_half[:, np.newaxis]

    # source position
    print(f"Posição da fonte: ")
    print(f"x = {xsource}")
    print(f"y = {ysource}\n")

    # Localizacao dos sensores (receptores)
    print(f"Sensor:")
    print(f"x_target, y_target = {xdeb}, {ydeb}")
    print(f"i, j = {sens_y}, {sens_x}")

    # Verifica a condicao de estabilidade de Courant
    # R. Courant et K. O. Friedrichs et H. Lewy (1928)
    courant_number = cp_unrelaxed * dt * np.sqrt(1.0 / dx ** 2 + 1.0 / dy ** 2)
    print(f"Número de Courant é {courant_number}")
    if courant_number > 1:
        print("O passo de tempo é muito longo, a simulação será instável")
        exit(1)

    # Configuracao e inicializacao da janela de exibicao
    App = pg.QtWidgets.QApplication([])
    window = Window()

    # Inicio do laco de tempo
    for it in range(nstep):
        # Calculo da primeira derivada espacial dividida pela densidade
        vdp_x[:, :-1] = (p_1[:, 1:] - p_1[:, :-1]) / dx  # p_1[ny, nx] deve ser guardado
        mdp_x = b_x_half * mdp_x + a_x_half * vdp_x  # mdp_x[ny, nx] deve ser guardado, b_x_half[nx] e a_x_half[nx] cte
        vdp_y[:-1, :] = (p_1[1:, :] - p_1[:-1, :]) / dy
        mdp_y = b_y_half_t * mdp_y + a_y_half_t * vdp_y  # mdp_y[ny, nx] deve ser guardado, b_y_half[ny] e a_y_half[ny] cte
        dp_x = (
                       vdp_x / k_x_half + mdp_x) / rho_half_x  # dp_x[ny, nx] deve ser guardado, K_x_half[nx] e rho_half_x[ny, nx] cte
        dp_y = (
                       vdp_y / k_y_half_t + mdp_y) / rho_half_y  # dp_y[ny, nx] deve ser guardado, K_y_half[ny] e rho_half_y[ny, nx] cte

        # Compute the second spatial derivatives
        vdp_xx[:, 1:] = (dp_x[:, 1:] - dp_x[:, :-1]) / dx
        dmdp_x = b_x * dmdp_x + a_x * vdp_xx  # dmdp_x[ny, nx] deve ser guardado, b_x[nx] e a_x[nx] cte
        vdp_yy[1:, :] = (dp_y[1:, :] - dp_y[:-1, :]) / dy
        dmdp_y = b_y_t * dmdp_y + a_y_t * vdp_yy  # dmdp_y[ny, nx] deve ser guardado, b_y[ny] e a_y[ny] cte
        v_x = vdp_xx / k_x + dmdp_x  # v_x[ny, nx] deve ser guardado
        v_y = vdp_yy / k_y_t + dmdp_y  # v_y[ny, nx] deve ser guardado

        # apply the time evolution scheme
        # we apply it everywhere, including at some points on the edges of the domain that have not be calculated above,
        # which is of course wrong (or more precisely undefined), but this does not matter because these values
        # will be erased by the Dirichlet conditions set on these edges below
        # p_0[ny, nx], p_2[ny, nx] devem ser guardados, kappa_unrelaxed[ny, nx], cp_unrelaxed e Kronecker_source[ny, nx] cte
        p_0 = 2.0 * p_1 - p_2 + \
              dt ** 2 * \
              ((v_x + v_y) * kappa_unrelaxed + 4.0 * math.pi * cp_unrelaxed ** 2 * source_term[it] * kronecker_source)

        ## apply Dirichlet conditions at the bottom of the C-PML layers
        ## which is the right condition to implement in order for C-PML to remain stable at long times
        # Dirichlet condition for pressure on the left boundary
        p_0[:, 0] = 0

        # Dirichlet condition for pressure on the right boundary
        p_0[:, nx - 1] = 0

        # Dirichlet condition for pressure on the bottom boundary
        p_0[0, :] = 0

        # Dirichlet condition for pressure on the top boundary
        p_0[ny - 1, :] = 0

        # print maximum of pressure and of norm of velocity
        pressurenorm = np.max(np.abs(p_0))
        print(f"Passo de tempo {it} de {nstep} passos")
        print(f"Tempo: {it * dt} seconds")
        print(f"Valor máximo absoluto da pressão = {pressurenorm}")

        # Verifica a estabilidade da simulacao
        if pressurenorm > STABILITY_THRESHOLD:
            print("Simulacao tornando-se instável")
            exit(2)

        window.imv.setImage(p_0.T, levels=[-1.0, 1.0])
        App.processEvents()

        # move new values to old values (the present becomes the past, the future becomes the present)
        p_2 = p_1
        p_1 = p_0

    App.exit()

    # End of the main loop
    print("Simulacao terminada.")


# --------------------------
# Shader [kernel] para a simulação em WEBGPU
shader_test = f"""
    struct SimIntValues {{
        y_sz: i32,          // Y field size
        x_sz: i32,          // X field size
        y_src: i32,         // Y source
        x_src: i32,         // X source
        y_sens: i32,        // Y sensor
        x_sens: i32,        // X sensor
        k: i32              // iteraction
    }};
    
    struct SimFltValues {{
        cp_unrelaxed: f32,  // sound speed
        dt: f32             // delta t
    }};

    // Group 0 - parameters
    @group(0) @binding(0)   // param_flt32
    var<storage,read> sim_flt_par: SimFltValues;

    @group(0) @binding(1) // source term
    var<storage,read> src: array<f32>;
    
    @group(0) @binding(2) // kronecker_src
    var<storage,read> kronecker_src: array<f32>;

    @group(0) @binding(3) // rho_half_x
    var<storage,read> rho_h_x: array<f32>;
    
    @group(0) @binding(4) // rho_half_y
    var<storage,read> rho_h_y: array<f32>;
    
    @group(0) @binding(5) // kappa
    var<storage,read> kappa: array<f32>;
    
    @group(0) @binding(6) // a_x
    var<storage,read> a_x: array<f32>;
    
    @group(0) @binding(7) // b_x
    var<storage,read> b_x: array<f32>;
    
    @group(0) @binding(8) // k_x
    var<storage,read> k_x: array<f32>;
    
    @group(0) @binding(9) // a_y
    var<storage,read> a_y: array<f32>;
    
    @group(0) @binding(10) // b_y
    var<storage,read> b_y: array<f32>;
    
    @group(0) @binding(11) // k_y
    var<storage,read> k_y: array<f32>;
    
    @group(0) @binding(12) // a_x_h
    var<storage,read> a_x_h: array<f32>;
    
    @group(0) @binding(13) // b_x_h
    var<storage,read> b_x_h: array<f32>;
    
    @group(0) @binding(14) // k_x_h
    var<storage,read> k_x_h: array<f32>;
    
    @group(0) @binding(15) // a_y_h
    var<storage,read> a_y_h: array<f32>;
    
    @group(0) @binding(16) // b_y_h
    var<storage,read> b_y_h: array<f32>;
    
    @group(0) @binding(17) // k_y_h
    var<storage,read> k_y_h: array<f32>;
    
    @group(0) @binding(18) // param_int32
    var<storage,read_write> sim_int_par: SimIntValues;

    // Group 1 - simulation arrays
    @group(1) @binding(19) // pressure field k+1
    var<storage,read_write> p_2: array<f32>;

    @group(1) @binding(20) // pressure field k
    var<storage,read_write> p_1: array<f32>;

    @group(1) @binding(21) // pressure field k-1
    var<storage,read_write> p_0: array<f32>;

    @group(1) @binding(22) // v_x
    var<storage,read_write> v_x: array<f32>;

    @group(1) @binding(23) // v_y
    var<storage,read_write> v_y: array<f32>;
    
    @group(1) @binding(24) // mdp_x
    var<storage,read_write> mdp_x: array<f32>;

    @group(1) @binding(25) // mdp_y
    var<storage,read_write> mdp_y: array<f32>;
    
    @group(1) @binding(26) // dp_x
    var<storage,read_write> dp_x: array<f32>;

    @group(1) @binding(27) // dp_y
    var<storage,read_write> dp_y: array<f32>;
    
    @group(1) @binding(28) // dmdp_x
    var<storage,read_write> dmdp_x: array<f32>;

    @group(1) @binding(29) // dmdp_y
    var<storage,read_write> dmdp_y: array<f32>;
    
    // Group 2 - sensors arrays
    @group(2) @binding(30) // sensor signal
    var<storage,read_write> sensor: array<f32>;

    // function to convert 2D [y,x] index into 1D [yx] index
    fn yx(y: i32, x: i32) -> i32 {{
        let index = x + y * sim_int_par.x_sz;

        return select(-1, index, y >= 0 && y < sim_int_par.y_sz && x >= 0 && x < sim_int_par.x_sz);
    }}
    
    // function to get a kappa array value
    fn get_kappa(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, kappa[index], index != -1);
    }}
    
    // function to get a kronecker_src array value
    fn get_kronecker_src(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, kronecker_src[index], index != -1);
    }}
    
    // function to get a rho_h_x array value
    fn get_rho_h_x(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, rho_h_x[index], index != -1);
    }}
    
    // function to get a rho_h_y array value
    fn get_rho_h_y(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, rho_h_y[index], index != -1);
    }}
    
    // function to get a src array value
    fn get_src(n: i32) -> f32 {{
        return select(0.0, src[n], n >= 0);
    }}
    
    // function to get a a_x array value
    fn get_a_x(n: i32) -> f32 {{
        return select(0.0, a_x[n], n >= 0);
    }}
    
    // function to get a b_x array value
    fn get_b_x(n: i32) -> f32 {{
        return select(0.0, b_x[n], n >= 0);
    }}
    
    // function to get a k_x array value
    fn get_k_x(n: i32) -> f32 {{
        return select(0.0, k_x[n], n >= 0);
    }}
    
    // function to get a a_y array value
    fn get_a_y(n: i32) -> f32 {{
        return select(0.0, a_y[n], n >= 0);
    }}
    
    // function to get a b_y array value
    fn get_b_y(n: i32) -> f32 {{
        return select(0.0, b_y[n], n >= 0);
    }}
    
    // function to get a k_y array value
    fn get_k_y(n: i32) -> f32 {{
        return select(0.0, k_y[n], n >= 0);
    }}
    
    // function to get a a_x_h array value
    fn get_a_x_h(n: i32) -> f32 {{
        return select(0.0, a_x_h[n], n >= 0);
    }}
    
    // function to get a b_x_h array value
    fn get_b_x_h(n: i32) -> f32 {{
        return select(0.0, b_x_h[n], n >= 0);
    }}
    
    // function to get a k_x_h array value
    fn get_k_x_h(n: i32) -> f32 {{
        return select(0.0, k_x_h[n], n >= 0);
    }}
    
    // function to get a a_y_h array value
    fn get_a_y_h(n: i32) -> f32 {{
        return select(0.0, a_y_h[n], n >= 0);
    }}
    
    // function to get a b_y_h array value
    fn get_b_y_h(n: i32) -> f32 {{
        return select(0.0, b_y_h[n], n >= 0);
    }}
    
    // function to get a k_y_h array value
    fn get_k_y_h(n: i32) -> f32 {{
        return select(0.0, k_y_h[n], n >= 0);
    }}

    // function to get an p_2 array value
    fn get_p_2(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, p_2[index], index != -1);
    }}

    // function to set a p_2 array value
    fn set_p_2(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            p_2[index] = val;
        }}
    }}

    // function to get an p_1 array value
    fn get_p_1(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, p_1[index], index != -1);
    }}

    // function to set a p_1 array value
    fn set_p_1(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            p_1[index] = val;
        }}
    }}

    // function to get an p_0 array value
    fn get_p_0(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, p_0[index], index != -1);
    }}

    // function to set a p_0 array value
    fn set_p_0(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            p_0[index] = val;
        }}
    }}
    
    // function to get an v_x array value
    fn get_v_x(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, v_x[index], index != -1);
    }}

    // function to set a v_x array value
    fn set_v_x(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            v_x[index] = val;
        }}
    }}
    
    // function to get an v_y array value
    fn get_v_y(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, v_y[index], index != -1);
    }}

    // function to set a v_y array value
    fn set_v_y(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            v_y[index] = val;
        }}
    }}
    
    // function to get an mdp_x array value
    fn get_mdp_x(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, mdp_x[index], index != -1);
    }}

    // function to set a mdp_x array value
    fn set_mdp_x(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            mdp_x[index] = val;
        }}
    }}
    
    // function to get an mdp_y array value
    fn get_mdp_y(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, mdp_y[index], index != -1);
    }}

    // function to set a mdp_y array value
    fn set_mdp_y(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            mdp_y[index] = val;
        }}
    }}
    
    // function to get an dp_x array value
    fn get_dp_x(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, dp_x[index], index != -1);
    }}

    // function to set a dp_x array value
    fn set_dp_x(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            dp_x[index] = val;
        }}
    }}
    
    // function to get an dp_y array value
    fn get_dp_y(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, dp_y[index], index != -1);
    }}

    // function to set a dp_y array value
    fn set_dp_y(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            dp_y[index] = val;
        }}
    }}
    
    // function to get an dmdp_x array value
    fn get_dmdp_x(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, dmdp_x[index], index != -1);
    }}

    // function to set a dmdp_x array value
    fn set_dmdp_x(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            dmdp_x[index] = val;
        }}
    }}
    
    // function to get an dmdp_y array value
    fn get_dmdp_y(y: i32, x: i32) -> f32 {{
        let index: i32 = yx(y, x);

        return select(0.0, dmdp_y[index], index != -1);
    }}

    // function to set a dmdp_y array value
    fn set_dmdp_y(y: i32, x: i32, val : f32) {{
        let index: i32 = yx(y, x);

        if(index != -1) {{
            dmdp_y[index] = val;
        }}
    }}

    // function to calculate derivatives
    @compute
    @workgroup_size({wsx}, {wsy})
    fn space_sim(@builtin(global_invocation_id) index: vec3<u32>) {{
        let y: i32 = i32(index.x);          // y thread index
        let x: i32 = i32(index.y);          // x thread index
        let idx: i32 = yx(y, x);

        // central
        //if(idx != -1) {{
        //    lap[idx] = 2.0 * coef[0] * getPKm1(z, x);
        //
        //    for (var i = 1; i < num_coef; i = i + 1) {{
        //        lap[idx] += coef[i] * (getPKm1(z - i, x) +  // i acima
        //                               getPKm1(z + i, x) +  // i abaixo
        //                               getPKm1(z, x - i) +  // i a esquerda
        //                               getPKm1(z, x + i));  // i a direita
        //    }}
        //}}
    }}

    @compute
    @workgroup_size(1)
    fn incr_k() {{
        sim_int_par.k += 1;
    }}

    @compute
    @workgroup_size({wsx}, {wsy})
    fn time_sim(@builtin(global_invocation_id) index: vec3<u32>) {{
        var add_src: f32 = 0.0;             // Source term
        let y: i32 = i32(index.x);          // y thread index
        let x: i32 = i32(index.y);          // x thread index
        let y_src: i32 = sim_int_par.y_src;         // source term y position
        let x_src: i32 = sim_int_par.x_src;         // source term x position
        let idx: i32 = yx(y, x);

        // --------------------
        // Update pressure field
        add_src = select(0.0, src[sim_int_par.k], y == y_src && x == x_src);
        //setPK(y, x, -1.0*getPKm2(z, x) + 2.0*getPKm1(z, x) + c[idx]*c[idx]*lap[idx] + add_src);

        // --------------------
        // Circular buffer
        set_p_2(y, x, get_p_1(y, x));
        set_p_1(y, x, get_p_0(y, x));

        if(y == sim_int_par.y_sens && x == sim_int_par.x_sens) {{
            sensor[sim_int_par.k] = get_p_2(y, x);
        }}
    }}
    """


# Simulação completa em WEB GPU
def sim_webgpu():
    global p_2, p_1, p_0, mdp_x, mdp_y, dp_x, dp_y, dmdp_x, dmdp_y, v_x, v_y, a_x, nstep

    # Arrays com parametros inteiros (i32) e ponto flutuante (f32) para rodar o simulador
    params_i32 = np.array([ny, nx, isource, jsource, sens_y, sens_x, 0], dtype=np.int32)
    params_f32 = np.array([cp_unrelaxed, dt], dtype=flt32)

    # =====================
    # webgpu configurations
    if gpu_type == "NVIDIA":
        device = wgpu.utils.get_default_device()
    else:
        adapter = wgpu.request_adapter(canvas=None, power_preference="low-power")
        device = adapter.request_device()

    cshader = device.create_shader_module(code=shader_test)

    # Definicao dos buffers que terao informacoes compartilhadas entre CPU e GPU
    # ------- Buffers para o binding de parametros -------------
    # Buffer de parametros com valores em ponto flutuante
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 0
    b_param_flt32 = device.create_buffer_with_data(data=params_f32,
                                                   usage=wgpu.BufferUsage.STORAGE |
                                                         wgpu.BufferUsage.COPY_SRC)

    # Termo de fonte
    # Binding 1
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    b_src = device.create_buffer_with_data(data=source_term,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # Mapa da posicao das fontes
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 2
    b_kronecker_src = device.create_buffer_with_data(data=kronecker_source,
                                                     usage=wgpu.BufferUsage.STORAGE |
                                                           wgpu.BufferUsage.COPY_SRC)

    # Densidade nos pontos intermediarios do grid (staggered grid)
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 3
    b_rho_half_x = device.create_buffer_with_data(data=rho_half_x,
                                                  usage=wgpu.BufferUsage.STORAGE |
                                                        wgpu.BufferUsage.COPY_SRC)

    # Densidade nos pontos intermediarios do grid (staggered grid)
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 4
    b_rho_half_y = device.create_buffer_with_data(data=rho_half_y,
                                                  usage=wgpu.BufferUsage.STORAGE |
                                                        wgpu.BufferUsage.COPY_SRC)
    # Mapa de rigidez (stiffness)
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 5
    b_kappa = device.create_buffer_with_data(data=kappa_unrelaxed,
                                             usage=wgpu.BufferUsage.STORAGE |
                                                   wgpu.BufferUsage.COPY_SRC)

    # Coeficientes de absorcao
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 6
    b_a_x = device.create_buffer_with_data(data=a_x,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 7
    b_b_x = device.create_buffer_with_data(data=b_x,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 8
    b_k_x = device.create_buffer_with_data(data=k_x,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 9
    b_a_y = device.create_buffer_with_data(data=a_y,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 10
    b_b_y = device.create_buffer_with_data(data=b_y,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 11
    b_k_y = device.create_buffer_with_data(data=k_y,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 12
    b_a_x_half = device.create_buffer_with_data(data=a_x_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 13
    b_b_x_half = device.create_buffer_with_data(data=b_x_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 14
    b_k_x_half = device.create_buffer_with_data(data=k_x_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 15
    b_a_y_half = device.create_buffer_with_data(data=a_y_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 16
    b_b_y_half = device.create_buffer_with_data(data=b_y_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 17
    b_k_y_half = device.create_buffer_with_data(data=k_y_half,
                                                usage=wgpu.BufferUsage.STORAGE |
                                                      wgpu.BufferUsage.COPY_SRC)

    # Buffer de parametros com valores inteiros
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 18
    b_param_int32 = device.create_buffer_with_data(data=params_i32,
                                                   usage=wgpu.BufferUsage.STORAGE |
                                                         wgpu.BufferUsage.COPY_SRC)

    # Buffers com os campos de pressao
    # Pressao futura (amostra de tempo n+1)
    # [STORAGE | COPY_DST | COPY_SRC] pois sao valores passados para a GPU e tambem retornam a CPU [COPY_DST]
    # Binding 19
    b_p_2 = device.create_buffer_with_data(data=p_2,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_DST |
                                                 wgpu.BufferUsage.COPY_SRC)
    # Pressao atual (amostra de tempo n)
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 20
    b_p_1 = device.create_buffer_with_data(data=p_1,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)
    # Pressao passada (amostra de tempo n-1)
    # [STORAGE | COPY_SRC] pois sao valores passados para a GPU, mas nao necessitam retornar a CPU
    # Binding 21
    b_p_0 = device.create_buffer_with_data(data=p_0,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)
    # Matrizes para o calculo das derivadas (primeira e segunda)
    # Binding 22
    b_v_x = device.create_buffer_with_data(data=v_x,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)
    # Binding 23
    b_v_y = device.create_buffer_with_data(data=v_y,
                                           usage=wgpu.BufferUsage.STORAGE |
                                                 wgpu.BufferUsage.COPY_SRC)
    # Binding 24
    b_mdp_x = device.create_buffer_with_data(data=mdp_x,
                                             usage=wgpu.BufferUsage.STORAGE |
                                                   wgpu.BufferUsage.COPY_SRC)
    # Binding 25
    b_mdp_y = device.create_buffer_with_data(data=mdp_y,
                                             usage=wgpu.BufferUsage.STORAGE |
                                                   wgpu.BufferUsage.COPY_SRC)
    # Binding 26
    b_dp_x = device.create_buffer_with_data(data=dp_x,
                                            usage=wgpu.BufferUsage.STORAGE |
                                                  wgpu.BufferUsage.COPY_SRC)
    # Binding 27
    b_dp_y = device.create_buffer_with_data(data=dp_y,
                                            usage=wgpu.BufferUsage.STORAGE |
                                                  wgpu.BufferUsage.COPY_SRC)
    # Binding 28
    b_dmdp_x = device.create_buffer_with_data(data=dmdp_x,
                                              usage=wgpu.BufferUsage.STORAGE |
                                                    wgpu.BufferUsage.COPY_SRC)
    # Binding 29
    b_dmdp_y = device.create_buffer_with_data(data=dmdp_y,
                                              usage=wgpu.BufferUsage.STORAGE |
                                                    wgpu.BufferUsage.COPY_SRC)

    # Sinal do sensor
    # [STORAGE | COPY_DST | COPY_SRC] pois sao valores passados para a GPU e tambem retornam a CPU [COPY_DST]
    # Binding 30
    b_sens = device.create_buffer_with_data(data=sensor,
                                            usage=wgpu.BufferUsage.STORAGE |
                                                  wgpu.BufferUsage.COPY_DST |
                                                  wgpu.BufferUsage.COPY_SRC)

    # Esquema de amarracao dos parametros (binding layouts [bl])
    # Parametros
    bl_params = [
        {"binding": i,
         "visibility": wgpu.ShaderStage.COMPUTE,
         "buffer": {
             "type": wgpu.BufferBindingType.read_only_storage}
         } for i in range(18)
    ]
    # b_param_i32
    bl_params.append({
                         "binding": 18,
                         "visibility": wgpu.ShaderStage.COMPUTE,
                         "buffer": {
                             "type": wgpu.BufferBindingType.storage,
                         },
                     })

    # Arrays da simulacao
    bl_sim_arrays = [
        {"binding": i,
         "visibility": wgpu.ShaderStage.COMPUTE,
         "buffer": {
             "type": wgpu.BufferBindingType.storage}
         } for i in range(19, 30)
    ]

    # Sensores
    bl_sensors = [
        {
            "binding": 30,
            "visibility": wgpu.ShaderStage.COMPUTE,
            "buffer": {
                "type": wgpu.BufferBindingType.storage,
            },
        },
    ]

    b_params = [
        {
            "binding": 0,
            "resource": {"buffer": b_param_flt32, "offset": 0, "size": b_param_flt32.size},
        },
        {
            "binding": 1,
            "resource": {"buffer": b_src, "offset": 0, "size": b_src.size},
        },
        {
            "binding": 2,
            "resource": {"buffer": b_kronecker_src, "offset": 0, "size": b_kronecker_src.size},
        },
        {
            "binding": 3,
            "resource": {"buffer": b_rho_half_x, "offset": 0, "size": b_rho_half_x.size},
        },
        {
            "binding": 4,
            "resource": {"buffer": b_rho_half_y, "offset": 0, "size": b_rho_half_y.size},
        },
        {
            "binding": 5,
            "resource": {"buffer": b_kappa, "offset": 0, "size": b_kappa.size},
        },
        {
            "binding": 6,
            "resource": {"buffer": b_a_x, "offset": 0, "size": b_a_x.size},
        },
        {
            "binding": 7,
            "resource": {"buffer": b_b_x, "offset": 0, "size": b_b_x.size},
        },
        {
            "binding": 8,
            "resource": {"buffer": b_k_x, "offset": 0, "size": b_k_x.size},
        },
        {
            "binding": 9,
            "resource": {"buffer": b_a_y, "offset": 0, "size": b_a_y.size},
        },
        {
            "binding": 10,
            "resource": {"buffer": b_b_y, "offset": 0, "size": b_b_y.size},
        },
        {
            "binding": 11,
            "resource": {"buffer": b_k_y, "offset": 0, "size": b_k_y.size},
        },
        {
            "binding": 12,
            "resource": {"buffer": b_a_x_half, "offset": 0, "size": b_a_x_half.size},
        },
        {
            "binding": 13,
            "resource": {"buffer": b_b_x_half, "offset": 0, "size": b_b_x_half.size},
        },
        {
            "binding": 14,
            "resource": {"buffer": b_k_x_half, "offset": 0, "size": b_k_x_half.size},
        },
        {
            "binding": 15,
            "resource": {"buffer": b_a_y_half, "offset": 0, "size": b_a_y_half.size},
        },
        {
            "binding": 16,
            "resource": {"buffer": b_b_y_half, "offset": 0, "size": b_b_y_half.size},
        },
        {
            "binding": 17,
            "resource": {"buffer": b_k_y_half, "offset": 0, "size": b_k_y_half.size},
        },
        {
            "binding": 18,
            "resource": {"buffer": b_param_int32, "offset": 0, "size": b_param_int32.size},
        },
    ]
    b_sim_arrays = [
        {
            "binding": 19,
            "resource": {"buffer": b_p_2, "offset": 0, "size": b_p_2.size},
        },
        {
            "binding": 20,
            "resource": {"buffer": b_p_1, "offset": 0, "size": b_p_1.size},
        },
        {
            "binding": 21,
            "resource": {"buffer": b_p_0, "offset": 0, "size": b_p_0.size},
        },
        {
            "binding": 22,
            "resource": {"buffer": b_v_x, "offset": 0, "size": b_v_x.size},
        },
        {
            "binding": 23,
            "resource": {"buffer": b_v_y, "offset": 0, "size": b_v_y.size},
        },
        {
            "binding": 24,
            "resource": {"buffer": b_mdp_x, "offset": 0, "size": b_mdp_x.size},
        },
        {
            "binding": 25,
            "resource": {"buffer": b_mdp_y, "offset": 0, "size": b_mdp_y.size},
        },
        {
            "binding": 26,
            "resource": {"buffer": b_dp_x, "offset": 0, "size": b_dp_x.size},
        },
        {
            "binding": 27,
            "resource": {"buffer": b_dp_y, "offset": 0, "size": b_dp_y.size},
        },
        {
            "binding": 28,
            "resource": {"buffer": b_dmdp_x, "offset": 0, "size": b_dmdp_x.size},
        },
        {
            "binding": 29,
            "resource": {"buffer": b_dmdp_y, "offset": 0, "size": b_dmdp_y.size},
        },
    ]
    b_sensors = [
        {
            "binding": 30,
            "resource": {"buffer": b_sens, "offset": 0, "size": b_sens.size},
        },
    ]

    # Put everything together
    bgl_0 = device.create_bind_group_layout(entries=bl_params)
    bgl_1 = device.create_bind_group_layout(entries=bl_sim_arrays)
    bgl_2 = device.create_bind_group_layout(entries=bl_sensors)
    pipeline_layout = device.create_pipeline_layout(bind_group_layouts=[bgl_0, bgl_1, bgl_2])
    bg_0 = device.create_bind_group(layout=bgl_0, entries=b_params)
    bg_1 = device.create_bind_group(layout=bgl_1, entries=b_sim_arrays)
    bg_2 = device.create_bind_group(layout=bgl_2, entries=b_sensors)

    # Create and run the pipeline
    compute_time_sim = device.create_compute_pipeline(layout=pipeline_layout,
                                                      compute={"module": cshader, "entry_point": "time_sim"})
    compute_space_sim = device.create_compute_pipeline(layout=pipeline_layout,
                                                       compute={"module": cshader, "entry_point": "space_sim"})
    compute_incr_k = device.create_compute_pipeline(layout=pipeline_layout,
                                                    compute={"module": cshader, "entry_point": "incr_k"})
    command_encoder = device.create_command_encoder()
    compute_pass = command_encoder.begin_compute_pass()

    compute_pass.set_bind_group(0, bg_0, [], 0, 999999)  # last 2 elements not used
    compute_pass.set_bind_group(1, bg_1, [], 0, 999999)  # last 2 elements not used
    compute_pass.set_bind_group(2, bg_2, [], 0, 999999)  # last 2 elements not used
    for i in range(nstep):
        compute_pass.set_pipeline(compute_time_sim)
        compute_pass.dispatch_workgroups(ny // wsx, nx // wsy)

        compute_pass.set_pipeline(compute_space_sim)
        compute_pass.dispatch_workgroups(ny // wsx, nx // wsy)

        compute_pass.set_pipeline(compute_incr_k)
        compute_pass.dispatch_workgroups(1)

    compute_pass.end()
    device.queue.submit([command_encoder.finish()])
    out = device.queue.read_buffer(b_p_2).cast("f")  # reads from buffer 3
    sens = np.array(device.queue.read_buffer(b_sens).cast("f"))
    adapter_info = device.adapter.request_adapter_info()
    return sens, adapter_info["device"]


times_webgpu = list()
times_cpu = list()

# WebGPU
if do_sim_gpu:
    for n in range(n_iter_gpu):
        print(f'Simulacao WEBGPU')
        print(f'Iteracao {n}')
        t_webgpu = time()
        sensor, gpu_str = sim_webgpu()
        times_webgpu.append(time() - t_webgpu)
        print(gpu_str)
        print(f'{times_webgpu[-1]:.3}s')

# CPU
if do_sim_cpu:
    for n in range(n_iter_cpu):
        print(f'SIMULAÇÃO CPU')
        print(f'Iteracao {n}')
        t_ser = time()
        sim_cpu()
        times_cpu.append(time() - t_ser)
        print(f'{times_cpu[-1]:.3}s')
#
# times_for = np.array(times_for)
# times_ser = np.array(times_ser)
# if do_sim_gpu:
#     print(f'workgroups X: {wsx}; workgroups Y: {wsy}')
#
# print(f'TEMPO - {nt} pontos de tempo')
# if do_sim_gpu and n_iter_gpu > 5:
#     print(f'GPU: {times_for[5:].mean():.3}s (std = {times_for[5:].std()})')
#
# if do_sim_cpu and n_iter_cpu > 5:
#     print(f'CPU: {times_ser[5:].mean():.3}s (std = {times_ser[5:].std()})')
#
# if do_sim_gpu and do_sim_cpu:
#     print(f'MSE entre as simulações: {mean_squared_error(u_ser, u_for)}')
#
# if plot_results:
#     if do_sim_gpu:
#         gpu_sim_result = plt.figure()
#         plt.title(f'GPU simulation ({nz}x{nx})')
#         plt.imshow(u_for, aspect='auto', cmap='turbo_r')
#
#         sensor_gpu_result = plt.figure()
#         plt.title(f'Sensor at z = {sens_z} and x = {sens_x}')
#         plt.plot(t, sensor)
#
#     if do_sim_cpu:
#         cpu_sim_result = plt.figure()
#         plt.title(f'CPU simulation ({nz}x{nx})')
#         plt.imshow(u_ser, aspect='auto', cmap='turbo_r')
#
#     if do_comp_fig_cpu_gpu and do_sim_cpu and do_sim_gpu:
#         comp_sim_result = plt.figure()
#         plt.title(f'CPU vs GPU ({gpu_type}) error simulation ({nz}x{nx})')
#         plt.imshow(u_ser - u_for, aspect='auto', cmap='turbo_r')
#         plt.colorbar()
#
#     if show_results:
#         plt.show()
#
# if save_results:
#     now = datetime.now()
#     name = f'results/result_{now.strftime("%Y%m%d-%H%M%S")}_{nz}x{nx}_{nt}_iter'
#     if plot_results:
#         if do_sim_gpu:
#             gpu_sim_result.savefig(name + '_gpu_' + gpu_type + '.png')
#             sensor_gpu_result.savefig(name + '_sensor_' + gpu_type + '.png')
#
#         if do_sim_cpu:
#             cpu_sim_result.savefig(name + 'cpu.png')
#
#         if do_comp_fig_cpu_gpu and do_sim_cpu and do_sim_gpu:
#             comp_sim_result.savefig(name + 'comp_cpu_gpu_' + gpu_type + '.png')
#
#     np.savetxt(name + '_GPU_' + gpu_type + '.csv', times_for, '%10.3f', delimiter=',')
#     np.savetxt(name + '_CPU.csv', times_ser, '%10.3f', delimiter=',')
#     with open(name + '_desc.txt', 'w') as f:
#         f.write('Parametros do ensaio\n')
#         f.write('--------------------\n')
#         f.write('\n')
#         f.write(f'Quantidade de iteracoes no tempo: {nt}\n')
#         f.write(f'Tamanho da ROI: {nz}x{nx}\n')
#         f.write(f'Refletores na ROI: {"Sim" if use_refletors else "Nao"}\n')
#         f.write(f'Simulacao GPU: {"Sim" if do_sim_gpu else "Nao"}\n')
#         if do_sim_gpu:
#             f.write(f'GPU: {gpu_str}\n')
#             f.write(f'Numero de simulacoes GPU: {n_iter_gpu}\n')
#             if n_iter_gpu > 5:
#                 f.write(f'Tempo medio de execucao: {times_for[5:].mean():.3}s\n')
#                 f.write(f'Desvio padrao: {times_for[5:].std()}\n')
#             else:
#                 f.write(f'Tempo execucao: {times_for[0]:.3}s\n')
#
#         f.write(f'Simulacao CPU: {"Sim" if do_sim_cpu else "Nao"}\n')
#         if do_sim_cpu:
#             f.write(f'Numero de simulacoes CPU: {n_iter_cpu}\n')
#             if n_iter_cpu > 5:
#                 f.write(f'Tempo medio de execucao: {times_ser[5:].mean():.3}s\n')
#                 f.write(f'Desvio padrao: {times_ser[5:].std()}\n')
#             else:
#                 f.write(f'Tempo execucao: {times_ser[0]:.3}s\n')
#
#         if do_sim_gpu and do_sim_cpu:
#             f.write(f'MSE entre as simulacoes: {mean_squared_error(u_ser, u_for)}')
