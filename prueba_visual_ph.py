#!/usr/bin/env python3
# prueba_visual_ph.py — GUI visual para prueba del control difuso de pH
#
# Cubre: llenado del tanque, configuración de histéresis, lazo difuso SISO
# Hardware: PCA9685 (CH3+CH4 = Bomba Neutralizador, CH5 = BombaNivel),
#           RK500-12 RS-485, XM125 I2C
#
# Requisitos:
#   sudo apt install python3-tk python3-lgpio
#   sudo pip3 install smbus2 pyserial --break-system-packages
#
# Uso:
#   DISPLAY=:0 sudo -E python3 prueba_visual_ph.py

import os, fcntl, struct, time, threading, sys, csv, signal, json
from datetime import datetime
import csv
from collections import deque

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    print("Falta tkinter:  sudo apt install python3-tk")
    sys.exit(1)

try:
    import lgpio
    _LGPIO = True
except ImportError:
    print("[AVISO] lgpio no disponible")
    _LGPIO = False

try:
    import serial
except ImportError:
    print("[ERROR] Falta pyserial:  sudo pip3 install pyserial --break-system-packages")
    sys.exit(1)

try:
    from smbus2 import SMBus, i2c_msg
    _SMBUS = True
except ImportError:
    print("[AVISO] smbus2 no disponible — sensor de nivel deshabilitado")
    _SMBUS = False

# ═══════════════════════════════════════════════════════════════════════════════
#  Hardware — constantes
# ═══════════════════════════════════════════════════════════════════════════════
GPIO_OE       = 17
GPIO_ZC       = 27
SERIAL_PORT   = '/dev/ttyAMA0'
SERIAL_BAUD   = 9600
PCA_ADDR      = 0x40
XM125_ADDR    = 0x52
QUERY_PH      = bytes.fromhex('030300000006C42A')

CH_BURBUJEO   = 2    # Bomba de aire / burbujeo
CH_NEUT_A     = 3    # Bomba Neutralizador 1 (en serie)
CH_NEUT_B     = 4    # Bomba Neutralizador 2 (en serie)
CH_TIRA_LED   = 5    # Tira LED
CH_BOMBA_VAC1 = 8    # BombaVaciado 1
CH_BOMBA_VAC2 = 10   # BombaVaciado 2

# ── Comandos de calibración hardware del RK500-12 (Modbus RTU, esclavo 0x03) ──
# Función 0x06 — Write Single Register
# Registro 0x0009: comando de calibración del sensor
# ⚠ Verificar dirección de registro en el datasheet del sensor antes de usar
# ── Comandos de calibración hardware del RK500-12 (Modbus RTU, esclavo 0x03) ──
_SLAVE   = 0x03
_CAL_REG = 0x0055   # Registro de calibración RK500-12
_CAL_VAL = {4: 0x0004, 7: 0x0007, 10: 0x000A}

def _modbus_w06(reg, val):
    """Construye y envía un frame Modbus RTU FC06 (write single register)."""
    frame = bytes([_SLAVE, 0x06, (reg >> 8) & 0xFF, reg & 0xFF,
                   (val >> 8) & 0xFF, val & 0xFF])
    crc = _crc(frame)
    frame += bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    if not ser:
        return False
    with _ser_lock:
        ser.reset_input_buffer()
        ser.write(frame)
        resp = ser.read(8)   # serial timeout=0.5 s gestiona la espera
    return len(resp) >= 6 and resp[0] == _SLAVE and resp[1] == 0x06

def cal_ph_4():
    ok = _modbus_w06(_CAL_REG, _CAL_VAL[4])
    _add_log("📍 Cal pH 4 → " + ("OK ✓" if ok else "SIN RESPUESTA ✗"))

def cal_ph_7():
    ok = _modbus_w06(_CAL_REG, _CAL_VAL[7])
    _add_log("📍 Cal pH 7 → " + ("OK ✓" if ok else "SIN RESPUESTA ✗"))

def cal_ph_10():
    ok = _modbus_w06(_CAL_REG, _CAL_VAL[10])
    _add_log("📍 Cal pH 10 → " + ("OK ✓" if ok else "SIN RESPUESTA ✗"))

# Calibración del XM125 — actualizables en runtime
_cal_vacio = 1000.0   # mm cuando el reactor está vacío
_cal_lleno =  200.0   # mm cuando el reactor está lleno
_CAL_FILE  = os.path.expanduser('~/calibracion_nivel.json')
_nivel_cal_ok = [False]   # True solo después de marcar vacío (o cargar archivo)

# Nivel mínimo confiable: por debajo de este % el sensor da reflexiones falsas
_NIVEL_MIN_CONFIABLE = 15.0   # % (≈ 20 % ± 5 %)

def _cargar_cal():
    global _cal_vacio, _cal_lleno
    try:
        with open(_CAL_FILE) as f:
            d = json.load(f)
            _cal_vacio = float(d.get('vacio', _cal_vacio))
            _cal_lleno = float(d.get('lleno', _cal_lleno))
        _nivel_cal_ok[0] = True   # calibración guardada = confiable
        print(f"[CAL] Cargada: vacío={_cal_vacio} mm  lleno={_cal_lleno} mm")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[CAL] {e}")

def _guardar_cal():
    try:
        with open(_CAL_FILE, 'w') as f:
            json.dump({'vacio': _cal_vacio, 'lleno': _cal_lleno}, f)
        print(f"[CAL] Guardada: vacío={_cal_vacio} mm  lleno={_cal_lleno} mm")
    except Exception as e:
        print(f"[CAL] Error guardando: {e}")

_cargar_cal()

# Buffer para filtro de mediana (rechaza picos aislados)
_dist_buf = deque(maxlen=7)

# Filtro de velocidad: rechaza lecturas que salten más de este % entre muestras
_MAX_NIVEL_DELTA  = 15.0   # % máximo de cambio entre lecturas consecutivas
_last_nivel_valid = [None]  # último nivel aceptado

# Tiempo máximo que puede estar corriendo la bomba de llenado (seguridad)
MAX_FILL_MIN = 15   # minutos
# Lecturas consecutivas requeridas antes de auto-stop
CONFIRM_READS = 3

# Histéresis de vaciado automático
NIVEL_VAC_ON  = 99.0   # % — enciende CH8+CH10 cuando el nivel supera este umbral
NIVEL_VAC_OFF = 96.0   # % — apaga  CH8+CH10 cuando el nivel baja a este valor

TS_S          = 5
T_PULSO_MAX   = 10
E_MAX         = 3.5
PH_MIN        = 4.0
PH_MAX        = 7.5

# ═══════════════════════════════════════════════════════════════════════════════
#  Lógica difusa SISO (idéntica al C++)
# ═══════════════════════════════════════════════════════════════════════════════
def _trimf(x, a, b, c):
    if x <= a or x >= c: return 0.0
    return (x-a)/(b-a) if x <= b else (c-x)/(c-b)

def _trapmf(x, a, b, c, d):
    if x <= a or x >= d: return 0.0
    if b <= x <= c: return 1.0
    return (x-a)/(b-a) if x < b else (d-x)/(d-c)

MFS_E = {
    # Rango de control fino: [0, 0.8] pH — errores mayores saturan directo a tp_max
    'N':  ([0.0,  0.0,  0.06],      _trimf ),   # nulo
    'PE': ([0.04, 0.15, 0.28],      _trimf ),   # pequeño
    'ME': ([0.22, 0.35, 0.50],      _trimf ),   # mediano
    'GE': ([0.42, 0.58, 0.75],      _trimf ),   # grande
    'MG': ([0.65, 0.80, 0.80, 0.80],_trapmf),  # muy grande → máximo
}
MFS_S = {
    'OFF':   ([0.0, 0.0,  1.5],           _trimf ),   # zona muerta
    'POCO':  ([3.0, 5.0,  7.0],           _trimf ),   # centro 5 s
    'MEDIO': ([5.5, 7.0,  9.0],           _trimf ),   # centro 7 s
    'MUCHO': ([7.5, 9.0, 10.0],           _trimf ),   # centro 9 s
    'MAX':   ([9.5, 10.0, 10.0, 10.0],    _trapmf),   # 10 s = Ts
}
REGLAS = [('N','OFF'),('PE','POCO'),('ME','MEDIO'),('GE','MUCHO'),('MG','MAX')]

def fuzzy(error, e_max=None):
    if e_max is None: e_max = E_MAX
    e = max(0.0, min(e_max, error))
    num = den = 0.0
    for i in range(71):
        x  = i * 10.0 / 70
        mu = max(min(fn(e, *p), fn2(x, *p2))
                 for (en, sn) in REGLAS
                 for p, fn in [MFS_E[en]] for p2, fn2 in [MFS_S[sn]])
        num += x * mu; den += mu
    return num / den if den > 0 else 0.0

# ═══════════════════════════════════════════════════════════════════════════════
#  PCA9685
# ═══════════════════════════════════════════════════════════════════════════════
fd_pca = None
pca_ok = False
try:
    fd_pca = os.open('/dev/i2c-1', os.O_RDWR)
    fcntl.ioctl(fd_pca, 0x0703, PCA_ADDR)
    os.write(fd_pca, bytes([0x00, 0x10])); time.sleep(0.001)
    os.write(fd_pca, bytes([0xFE, 0x79])); time.sleep(0.001)
    os.write(fd_pca, bytes([0x00, 0x20])); time.sleep(0.001)
    time.sleep(0.001)
    os.write(fd_pca, bytes([0x00, 0xA0])); time.sleep(0.001)
    pca_ok = True
    print("[PCA9685] OK")
except Exception as e:
    print(f"[PCA9685] {e}")

def _pca_raw(canal, valor):
    if not pca_ok: return
    reg = 0x06 + canal * 4
    v   = max(0, min(4095, int(valor)))
    try: os.write(fd_pca, bytes([reg, 0x00, 0x00, v & 0xFF, v >> 8]))
    except: pass

def _pca_off(canal):
    if not pca_ok: return
    reg = 0x06 + canal * 4
    try: os.write(fd_pca, bytes([reg, 0x00, 0x00, 0x00, 0x10]))
    except: pass

def burbujeo(activo: bool):
    if activo: _pca_raw(CH_BURBUJEO, 4095)
    else:      _pca_off(CH_BURBUJEO)

def neutraliz_on():
    _pca_raw(CH_NEUT_A, 4095)
    _pca_raw(CH_NEUT_B, 4095)

def neutraliz_off():
    _pca_off(CH_NEUT_A)
    _pca_off(CH_NEUT_B)

def tira_led(activo: bool):
    if activo: _pca_raw(CH_TIRA_LED, 4095)
    else:      _pca_off(CH_TIRA_LED)

def bomba_vac1(activo: bool):
    if activo: _pca_raw(CH_BOMBA_VAC1, 4095)
    else:      _pca_off(CH_BOMBA_VAC1)

def bomba_vac2(activo: bool):
    if activo: _pca_raw(CH_BOMBA_VAC2, 4095)
    else:      _pca_off(CH_BOMBA_VAC2)

# ═══════════════════════════════════════════════════════════════════════════════
#  lgpio — OE + ZC
# ═══════════════════════════════════════════════════════════════════════════════
h = None
fase_viva = [True]
if _LGPIO:
    try:
        h = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(h, GPIO_OE, 0, 0)
        lgpio.gpio_claim_input(h, GPIO_ZC, lgpio.SET_PULL_UP)
        def _zc():
            prev = 0
            while fase_viva[0]:
                curr = lgpio.gpio_read(h, GPIO_ZC)
                if curr == 1 and prev == 0:
                    lgpio.gpio_write(h, GPIO_OE, 1)
                    time.sleep(50e-6)
                    lgpio.gpio_write(h, GPIO_OE, 0)
                prev = curr
                time.sleep(10e-6)
        threading.Thread(target=_zc, daemon=True).start()
        print("[lgpio] ZC activo")
    except Exception as e:
        print(f"[lgpio] {e}"); h = None

# ═══════════════════════════════════════════════════════════════════════════════
#  RS-485 — RK500-12
# ═══════════════════════════════════════════════════════════════════════════════
try:
    ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.5)
    print(f"[Serial] {SERIAL_PORT}")
except Exception as e:
    ser = None; print(f"[Serial] {e}")

_ser_lock = threading.Lock()

def _crc(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc

def leer_ph():
    if not ser: return None
    with _ser_lock:
        ser.reset_input_buffer()
        ser.write(QUERY_PH)
        r = ser.read(64)   # serial timeout=0.5 s gestiona la espera
    if len(r) < 15 or _crc(r[:-2]) != (r[-2] | r[-1] << 8): return None
    return struct.unpack('>f', r[3:7])[0], struct.unpack('>f', r[11:15])[0]

# ═══════════════════════════════════════════════════════════════════════════════
#  XM125 — nivel
# ═══════════════════════════════════════════════════════════════════════════════
bus = SMBus(1) if _SMBUS else None
xm_ok = False

def _xm_w(reg, val):
    bus.i2c_rdwr(i2c_msg.write(XM125_ADDR, [
        (reg>>8)&0xFF, reg&0xFF,
        (val>>24)&0xFF,(val>>16)&0xFF,(val>>8)&0xFF,val&0xFF]))

def _xm_r(reg):
    w = i2c_msg.write(XM125_ADDR, [(reg>>8)&0xFF, reg&0xFF])
    r = i2c_msg.read(XM125_ADDR, 4)
    bus.i2c_rdwr(w, r)
    d = list(r)
    return (d[0]<<24)|(d[1]<<16)|(d[2]<<8)|d[3]

_xm_warmup_until = [0.0]   # lecturas ignoradas hasta este timestamp

if _SMBUS:
    try:
        _xm_w(0x0040, 110); _xm_w(0x0041, 1500); _xm_w(0x0100, 1)
        for _ in range(50):
            time.sleep(0.1)
            if not (_xm_r(0x0003) & 0x80000000):
                xm_ok = True
                _xm_warmup_until[0] = time.time() + 10.0   # ignorar 1os 10 s
                print("[XM125] Calibrado OK — warmup 10 s")
                break
    except Exception as e:
        print(f"[XM125] {e}")

def _leer_dist_raw():
    """Lee la distancia mínima válida del XM125 en mm, o None si falla."""
    if not xm_ok: return None
    # Warmup: ignorar lecturas inestables al inicio
    if time.time() < _xm_warmup_until[0]:
        return None
    try:
        _xm_w(0x0100, 2)
        for _ in range(20):
            time.sleep(0.05)
            if not (_xm_r(0x0003) & 0x80000000): break
        res  = _xm_r(0x0010)
        n    = res & 0x0F
        if n == 0:
            return None
        picos = [_xm_r(0x0011 + j) for j in range(n)]
        # Rango válido: mínimo 110 mm (blind zone del sensor) hasta vacío + margen
        lo = max(110, min(_cal_lleno, _cal_vacio) - 50)
        hi = max(_cal_lleno, _cal_vacio) + 50
        cands = [d for d in picos if lo <= d <= hi]
        if not cands:
            # Log de picos rechazados para diagnóstico
            _add_log(f"⚠ XM125 sin pico válido — raw={picos} rango=[{lo},{hi}]mm")
            return None
        return min(cands)   # el pico más cercano = superficie del líquido
    except Exception as e:
        _add_log(f"⚠ XM125 error lectura: {e}")
        return None

def _dist_a_nivel(dist_mm):
    """Convierte distancia (mm) a nivel (%) usando calibración actual."""
    rango = _cal_vacio - _cal_lleno
    if abs(rango) < 10: return None   # calibración inválida
    nivel = ((_cal_vacio - dist_mm) / rango) * 100.0
    return max(0.0, min(100.0, nivel))

def leer_nivel():
    """Retorna (nivel_pct, dist_raw_mm) con mediana + filtro de velocidad.

    Devuelve (None, dist) si:
      - El usuario aún no ha marcado el punto vacío (_nivel_cal_ok = False)
      - El nivel calculado está por debajo de _NIVEL_MIN_CONFIABLE (zona de ruido)
    En esos casos el dist_raw sigue visible en UI pero no alimenta el filtro.
    """
    dist = _leer_dist_raw()
    if dist is None:
        return None, None

    # Guarda 1: calibración no confirmada por el usuario
    if not _nivel_cal_ok[0]:
        return None, dist   # mostrar distancia cruda pero no nivel

    nivel_raw = _dist_a_nivel(dist)
    if nivel_raw is None:
        return None, dist

    # Guarda 2: zona de nivel baja — reflexiones no confiables
    if nivel_raw < _NIVEL_MIN_CONFIABLE:
        return None, dist

    # A partir de aquí la lectura es confiable — filtrar y validar velocidad
    prev = _last_nivel_valid[0]
    if prev is not None and abs(nivel_raw - prev) > _MAX_NIVEL_DELTA:
        with _lock:
            state['sensor_spike'] = True
        return prev, dist

    with _lock:
        state['sensor_spike'] = False
    _dist_buf.append(dist)
    dist_filt = sorted(_dist_buf)[len(_dist_buf) // 2]   # mediana
    nivel = _dist_a_nivel(dist_filt)
    _last_nivel_valid[0] = nivel
    return nivel, dist

# ═══════════════════════════════════════════════════════════════════════════════
#  Estado compartido
# ═══════════════════════════════════════════════════════════════════════════════
_lock = threading.Lock()
state = {
    'ph':        None, 'temp':      None,
    'nivel':     None,
    'dist_raw':  None,
    'sp':        6.5,  'nivel_max': 100.0, 'nivel_hist': 95.0, 'ts_s': 30, 'banda': 0.1, 'k_pulso': 2.0, 'tp_max': 20.0, 'e_max': 0.8, 'e_sat': 0.8,
    'control_on': False,
    'led':        False,
    'burbujeo':   False,
    'sensor_spike': False,
    'bomba_vac1': False,
    'bomba_vac2': False,
    'bomba_neut': False,
    'error':      None,
    't_pulso':    None,
    'ciclo_cnt':  0,
    'registros':  [],
    'log':        [],
}

def _add_log(msg):
    ts  = datetime.now().strftime("%H:%M:%S")
    txt = f"{ts}  {msg}"
    with _lock:
        state['log'].append(txt)
        if len(state['log']) > 200:
            state['log'].pop(0)

# ═══════════════════════════════════════════════════════════════════════════════
#  Hilo de sensores (cada 3 s)
# ═══════════════════════════════════════════════════════════════════════════════
_stop = threading.Event()

def _hilo_sensores():
    while not _stop.is_set():
        r = leer_ph()
        if r:
            with _lock:
                state['ph']   = r[0]
                state['temp'] = r[1]
        nivel, dist = leer_nivel()
        with _lock:
            if nivel is not None: state['nivel'] = nivel
            if dist  is not None: state['dist_raw'] = dist
        _stop.wait(3)

# ═══════════════════════════════════════════════════════════════════════════════
#  Hilo de control pH (Ts = 5 s, tick = 1 s)
# ═══════════════════════════════════════════════════════════════════════════════
_t_pulso_rest  = [0]
_vac_auto      = [False]   # True si CH8+CH10 activadas por vaciado automático
_llenado_auto  = [False]   # True si CH3+CH4 activadas por nivel bajo (< nivel_hist)
_dyn_emax      = [None]    # e_max dinámico: baja 0.02/ciclo si error en [0.4, 0.5]

def _hilo_control():
    while not _stop.is_set():

        # ── Histéresis de vaciado (corre cada segundo, independiente del lazo pH) ──
        with _lock:
            nivel_now = state['nivel']
            n_hist_now = state['nivel_hist']
        if nivel_now is not None:
            if not _vac_auto[0] and nivel_now >= NIVEL_VAC_ON:
                _vac_auto[0] = True
                bomba_vac1(True)
                with _lock:
                    state['bomba_vac1'] = True
                _add_log(f"🔴 AUTO-VACIADO ON  nivel={nivel_now:.1f}% ≥ {NIVEL_VAC_ON:.0f}% — control pH inhibido")
            elif _vac_auto[0] and nivel_now <= NIVEL_VAC_OFF:
                _vac_auto[0] = False
                bomba_vac1(False)
                with _lock:
                    state['bomba_vac1'] = False
                _add_log(f"🟢 AUTO-VACIADO OFF nivel={nivel_now:.1f}% ≤ {NIVEL_VAC_OFF:.0f}% — control pH reanudado")

        # ── Auto-llenado: si nivel < nivel_hist, llenar antes de habilitar control ──
        # El sensor puede ser None si está por debajo de la zona confiable;
        # en ese caso también activamos llenado preventivo.
        with _lock:
            nivel_now = state['nivel']
            n_hist_now = state['nivel_hist']
        nivel_bajo = (nivel_now is None) or (nivel_now < n_hist_now)
        if nivel_bajo and not _vac_auto[0]:
            if not _llenado_auto[0]:
                _llenado_auto[0] = True
                neutraliz_on()
                with _lock:
                    state['bomba_neut'] = True
                _add_log(f"🟡 AUTO-LLENADO ON  nivel={'---' if nivel_now is None else f'{nivel_now:.1f}%'} < {n_hist_now:.0f}% — control pH inhibido hasta llenar")
        elif _llenado_auto[0] and nivel_now is not None and nivel_now >= n_hist_now:
            _llenado_auto[0] = False
            neutraliz_off()
            with _lock:
                state['bomba_neut'] = False
            _add_log(f"🟢 AUTO-LLENADO OFF nivel={nivel_now:.1f}% ≥ {n_hist_now:.0f}% — control pH habilitado")

        with _lock:
            activo = state['control_on']
        if activo:
            with _lock:
                state['ciclo_cnt'] += 1
                cnt = state['ciclo_cnt']

            # Gestionar pulso activo
            if _t_pulso_rest[0] > 0:
                _t_pulso_rest[0] -= 1
                if not state['bomba_neut']:
                    with _lock: state['bomba_neut'] = True
                    neutraliz_on()
                    _add_log("🔵 Bomba CH3+CH4 ON")
                if _t_pulso_rest[0] == 0:
                    with _lock: state['bomba_neut'] = False
                    neutraliz_off()
                    _add_log("⚫ Bomba CH3+CH4 OFF")

            # Evaluar cada Ts (configurable desde GUI)
            with _lock: ts_s = state['ts_s']
            if cnt >= ts_s:
                with _lock:
                    state['ciclo_cnt'] = 0
                    ph     = state['ph']
                    sp     = state['sp']
                    nivel  = state['nivel']
                    n_max  = state['nivel_max']

                if ph is None:
                    _add_log("⚠ Sin lectura de pH — ciclo omitido")
                    _stop.wait(1); continue

                if _llenado_auto[0]:
                    _add_log(f"CICLO pH={ph:.3f} — inhibido por auto-llenado (nivel bajo)")
                    _stop.wait(1); continue

                if _vac_auto[0]:
                    _add_log(f"CICLO pH={ph:.3f} — inhibido por vaciado automático")
                    _stop.wait(1); continue

                error = sp - ph
                with _lock:
                    state['error'] = error

                with _lock: banda = state['banda']
                nivel_ok = (nivel is None) or (nivel < n_max)
                if error <= banda:   # banda muerta configurable
                    _add_log(f"CICLO pH={ph:.3f} e={error:+.3f} → PRE-FILTRO")
                    with _lock:
                        state['t_pulso'] = 0.0
                        state['registros'].append({
                            'ts': datetime.now().isoformat(timespec='seconds'),
                            'ph': round(ph,3), 'sp': sp, 'error': round(error,3),
                            't_pulso': 0.0, 'accion': 'prefiltro'})
                elif not nivel_ok:
                    _add_log(f"CICLO pH={ph:.3f} e={error:+.3f} → GUARDA NIVEL ({nivel:.1f}% ≥ {n_max}%)")
                    with _lock: state['t_pulso'] = 0.0
                else:
                    with _lock:
                        k_pulso    = state['k_pulso']
                        tp_max     = state['tp_max']
                        e_max_base = state['e_max']
                        e_sat      = state['e_sat']
                    # Saturación directa para errores grandes
                    if error >= e_sat:
                        tp = float(tp_max)
                        _dyn_emax[0] = e_max_base   # reset
                    else:
                        # e_max dinámico: baja 0.02/ciclo si error estancado en [0.4, 0.5]
                        if _dyn_emax[0] is None:
                            _dyn_emax[0] = e_max_base
                        if 0.4 <= error <= 0.5:
                            _dyn_emax[0] = max(0.2, _dyn_emax[0] - 0.02)
                        else:
                            _dyn_emax[0] = e_max_base
                        tp = fuzzy(error, _dyn_emax[0]) * k_pulso
                    tp = max(0.0, min(float(tp_max), tp))
                    if 0 < tp < 1.0:
                        tp = 1.0   # mínimo 1 s por inercia de la bomba
                    tps = round(tp)
                    with _lock:
                        state['t_pulso'] = tp
                        _t_pulso_rest[0] = tps
                        state['registros'].append({
                            'ts': datetime.now().isoformat(timespec='seconds'),
                            'ph': round(ph,3), 'sp': sp, 'error': round(error,3),
                            't_pulso': round(tp,2), 'accion': f'pulso_{tps}s'})
                    _add_log(f"CICLO pH={ph:.3f} e={error:+.3f} → t_pulso={tp:.2f}s")
        else:
            # Control detenido — solo cancelar pulso automático pendiente
            if _t_pulso_rest[0] > 0:
                _t_pulso_rest[0] = 0
                with _lock: state['bomba_neut'] = False
                neutraliz_off()

        _stop.wait(1)

# ═══════════════════════════════════════════════════════════════════════════════
#  Exportar registros a CSV
# ═══════════════════════════════════════════════════════════════════════════════
_CSV_DIR = os.path.expanduser('~/biorreactor_datos')

def nueva_prueba():
    """Limpia el buffer de registros para empezar una prueba nueva sin reiniciar."""
    with _lock:
        n = len(state['registros'])
        state['registros'].clear()
    _dyn_emax[0] = None   # reset gain dinámico
    _add_log(f"🗑 Buffer limpiado ({n} ciclos descartados) — lista para nueva prueba")

def guardar_csv():
    """Guarda state['registros'] en ~/biorreactor_datos/YYYY-MM-DD_HHMMSS.csv"""
    with _lock:
        recs = list(state['registros'])
    if not recs:
        _add_log("⚠ Sin datos que exportar")
        return
    os.makedirs(_CSV_DIR, exist_ok=True)
    fname = os.path.join(_CSV_DIR, datetime.now().strftime('%Y-%m-%d_%H%M%S') + '.csv')
    with open(fname, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['ts','ph','sp','error','t_pulso','accion'])
        w.writeheader()
        w.writerows(recs)
    _add_log(f"💾 CSV guardado: {fname}  ({len(recs)} ciclos)")
    return fname

# ═══════════════════════════════════════════════════════════════════════════════
#  Limpieza
# ═══════════════════════════════════════════════════════════════════════════════
def _limpiar():
    _stop.set()
    _vac_auto[0]     = False
    _llenado_auto[0] = False
    neutraliz_off()
    burbujeo(False)
    tira_led(False)
    bomba_vac1(False)
    bomba_vac2(False)
    fase_viva[0] = False
    if h:
        try: lgpio.gpio_write(h, GPIO_OE, 1)
        except: pass
        try: lgpio.gpiochip_close(h)
        except: pass
    if fd_pca:
        try: os.close(fd_pca)
        except: pass
    if bus:
        try: bus.close()
        except: pass
    if ser:
        try: ser.close()
        except: pass
    # Guardar CSV
    with _lock:
        regs = list(state['registros'])
    if regs:
        fn = os.path.join(os.path.dirname(__file__),
             f"prueba_ph_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        with open(fn, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['ts','ph','sp','error','t_pulso','accion'])
            w.writeheader(); w.writerows(regs)
        print(f"\nCSV guardado: {fn}")

# ═══════════════════════════════════════════════════════════════════════════════
#  GUI — tkinter
# ═══════════════════════════════════════════════════════════════════════════════
class App:
    C_BG   = "#1e1e2e"
    C_CARD = "#2a2a3e"
    C_ACNT = "#6E9C9C"
    C_GRN  = "#4CAF50"
    C_RED  = "#ef4444"
    C_YLW  = "#f59e0b"
    C_TXT  = "#e2e8f0"
    C_DIM  = "#94a3b8"
    FONT_H = ("DejaVu Sans", 13, "bold")
    FONT_N = ("DejaVu Sans", 12)
    FONT_S = ("DejaVu Sans", 10)
    FONT_M = ("DejaVu Sans Mono", 11)

    def __init__(self, root):
        self.root = root
        root.title("Prueba Control pH — Biorreactor")
        root.configure(bg=self.C_BG)
        root.geometry("1100x700")
        root.resizable(True, True)

        self._build_ui()

        # Iniciar hilos
        threading.Thread(target=_hilo_sensores, daemon=True).start()
        threading.Thread(target=_hilo_control,  daemon=True).start()

        # Actualizar UI
        self.root.after(500, self._tick)

    # ── Construcción de la UI ──────────────────────────────────────────────────
    def _build_ui(self):
        # ── Barra superior ──────────────────────────────────────────────────────
        top = tk.Frame(self.root, bg="#0f0f1a", pady=6)
        top.pack(fill=tk.X)
        tk.Label(top, text="PRUEBA CONTROL pH — BIORREACTOR IPN",
                 bg="#0f0f1a", fg=self.C_ACNT,
                 font=("DejaVu Sans", 14, "bold")).pack(side=tk.LEFT, padx=14)
        self.lbl_estado = tk.Label(top, text="● INICIANDO",
                                   bg="#0f0f1a", fg=self.C_YLW,
                                   font=self.FONT_H)
        self.lbl_estado.pack(side=tk.RIGHT, padx=14)

        # ── Cuerpo principal ───────────────────────────────────────────────────
        body = tk.Frame(self.root, bg=self.C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        # Col izquierda (sensores + config)
        col_l = tk.Frame(body, bg=self.C_BG)
        col_l.pack(side=tk.LEFT, fill=tk.BOTH, padx=(0,6))

        self._card_sensores(col_l)
        self._card_calibracion_ph(col_l)
        self._card_calibracion(col_l)
        self._card_config(col_l)
        self._card_preparacion(col_l)

        # Col derecha (control + log)
        col_r = tk.Frame(body, bg=self.C_BG)
        col_r.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._card_control(col_r)
        self._card_log(col_r)

    def _card(self, parent, titulo, **kw):
        f = tk.LabelFrame(parent, text=titulo, bg=self.C_CARD, fg=self.C_ACNT,
                          font=self.FONT_H, bd=2, relief=tk.GROOVE,
                          padx=10, pady=8, **kw)
        f.pack(fill=tk.X, pady=4)
        return f

    # ── Card sensores ──────────────────────────────────────────────────────────
    def _card_sensores(self, parent):
        f = self._card(parent, "Sensores en tiempo real")
        for etiq, attr in [("pH", 'lbl_ph'), ("Nivel", 'lbl_niv'),
                           ("Dist. raw", 'lbl_dist'), ("Temp.", 'lbl_tmp')]:
            row = tk.Frame(f, bg=self.C_CARD)
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=etiq, width=8, anchor='w',
                     bg=self.C_CARD, fg=self.C_DIM, font=self.FONT_N).pack(side=tk.LEFT)
            lbl = tk.Label(row, text="---", width=14, anchor='e',
                           bg=self.C_CARD, fg=self.C_TXT, font=("DejaVu Sans", 13, "bold"))
            lbl.pack(side=tk.LEFT)
            setattr(self, attr, lbl)

        # Barra de nivel
        tk.Label(f, text="Nivel %", bg=self.C_CARD, fg=self.C_DIM,
                 font=self.FONT_S, anchor='w').pack(fill=tk.X, pady=(6,1))
        self.canvas_nivel = tk.Canvas(f, height=22, bg="#1e1e2e",
                                      highlightthickness=0)
        self.canvas_nivel.pack(fill=tk.X, pady=(0,2))

    # ── Card calibración pH (comandos hardware al RK500-12) ──────────────────
    def _card_calibracion_ph(self, parent):
        f = self._card(parent, "Calibración sensor pH  (RK500-12)")

        tk.Label(f,
            text="Sumergir sensor en buffer → esperar ~30 s → presionar botón",
            bg=self.C_CARD, fg=self.C_DIM, font=self.FONT_S
        ).pack(anchor='w', pady=(0, 6))

        row = tk.Frame(f, bg=self.C_CARD)
        row.pack(fill=tk.X)
        for txt, cmd in [
            ("⬛ Calibrar pH 4",  lambda: threading.Thread(target=cal_ph_4,  daemon=True).start()),
            ("⬛ Calibrar pH 7",  lambda: threading.Thread(target=cal_ph_7,  daemon=True).start()),
            ("⬛ Calibrar pH 10", lambda: threading.Thread(target=cal_ph_10, daemon=True).start()),
        ]:
            tk.Button(row, text=txt, command=cmd,
                bg="#374151", fg=self.C_TXT,
                font=self.FONT_S, relief=tk.FLAT, padx=10, pady=5
            ).pack(side=tk.LEFT, padx=(0, 6))

    # ── Card calibración XM125 ────────────────────────────────────────────────
    def _card_calibracion(self, parent):
        f = self._card(parent, "Calibración sensor nivel")

        # Mostrar valores actuales
        self.lbl_cal = tk.Label(f,
            text=f"Vacío: {_cal_vacio:.0f} mm   Lleno: {_cal_lleno:.0f} mm",
            bg=self.C_CARD, fg=self.C_DIM, font=self.FONT_S)
        self.lbl_cal.pack(anchor='w', pady=(0,4))

        row = tk.Frame(f, bg=self.C_CARD)
        row.pack(fill=tk.X)

        tk.Button(row, text="📍 Marcar VACÍO",
                  command=self._cal_marcar_vacio,
                  bg="#374151", fg=self.C_TXT,
                  font=self.FONT_S, relief=tk.FLAT, padx=8, pady=4
                  ).pack(side=tk.LEFT, padx=(0,4))

        tk.Button(row, text="📍 Marcar LLENO",
                  command=self._cal_marcar_lleno,
                  bg="#374151", fg=self.C_TXT,
                  font=self.FONT_S, relief=tk.FLAT, padx=8, pady=4
                  ).pack(side=tk.LEFT, padx=(0,4))

        tk.Button(row, text="💾 Guardar",
                  command=self._cal_guardar,
                  bg=self.C_ACNT, fg="black",
                  font=self.FONT_S, relief=tk.FLAT, padx=8, pady=4
                  ).pack(side=tk.LEFT)

        # Entrada manual si quieren teclear directo
        row2 = tk.Frame(f, bg=self.C_CARD)
        row2.pack(fill=tk.X, pady=(4,0))
        for etiq, attr in [("Vacío mm", '_cal_v_var'), ("Lleno mm", '_cal_l_var')]:
            tk.Label(row2, text=etiq, bg=self.C_CARD, fg=self.C_DIM,
                     font=self.FONT_S).pack(side=tk.LEFT)
            var = tk.StringVar(value="")
            setattr(self, attr, var)
            tk.Entry(row2, textvariable=var, width=6,
                     bg="#3a3a50", fg=self.C_TXT, insertbackground=self.C_TXT,
                     font=self.FONT_S, bd=0).pack(side=tk.LEFT, padx=(2,8))
        tk.Button(row2, text="✓ Aplicar",
                  command=self._cal_aplicar_manual,
                  bg=self.C_ACNT, fg="black",
                  font=self.FONT_S, relief=tk.FLAT, padx=6).pack(side=tk.LEFT)

    # ── Card configuración ─────────────────────────────────────────────────────
    def _card_config(self, parent):
        f = self._card(parent, "Configuración")
        params = [
            ("Setpoint pH",  'sp',         4.0,  7.5,  "sp_var"),
            ("Ts (s)",       'ts_s',        5.0, 60.0,  "ts_var"),
            ("Banda muerta", 'banda',       0.0,  0.5,  "banda_var"),
            ("K pulso",      'k_pulso',     0.5,  5.0,  "kp_var"),
            ("Pulso máx (s)",'tp_max',      5.0, 55.0,  "tpmax_var"),
            ("Nivel Máx. %", 'nivel_max',  50.0, 100.0, "nmax_var"),
            ("Nivel Mín. %", 'nivel_hist', 10.0,  95.0, "nhist_var"),
            ("Error máx (pH)",'e_max',      0.3,   3.5,  "emax_var"),
            ("Saturación (pH)",'e_sat',     0.3,   3.5,  "esat_var"),
        ]
        for etiq, key, lo, hi, var_attr in params:
            row = tk.Frame(f, bg=self.C_CARD)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=etiq, width=14, anchor='w',
                     bg=self.C_CARD, fg=self.C_DIM, font=self.FONT_N).pack(side=tk.LEFT)
            var = tk.StringVar(value=str(state[key]))
            setattr(self, var_attr, var)
            entry = tk.Entry(row, textvariable=var, width=7,
                             bg="#3a3a50", fg=self.C_TXT, insertbackground=self.C_TXT,
                             font=self.FONT_N, bd=0, relief=tk.FLAT)
            entry.pack(side=tk.LEFT, padx=4)
            # closure para key, lo, hi
            def _set(e=None, k=key, l=lo, h=hi, v=var):
                try:
                    val = float(v.get())
                    val = max(l, min(h, val))
                    with _lock: state[k] = val
                    v.set(f"{val:.1f}")
                    _add_log(f"Config: {k} = {val:.1f}")
                except ValueError:
                    pass
            entry.bind('<Return>', _set)
            tk.Button(row, text="✓", command=_set,
                      bg=self.C_ACNT, fg="black", font=self.FONT_S,
                      relief=tk.FLAT, padx=4).pack(side=tk.LEFT)

    # ── Card preparación del tanque ───────────────────────────────────────────
    def _card_preparacion(self, parent):
        f = self._card(parent, "Preparación del tanque")

        def _pump_row(label, btn_attr, target_attr, default, toggle_cmd, auto_cmd):
            row = tk.Frame(f, bg=self.C_CARD)
            row.pack(fill=tk.X, pady=3)
            btn = tk.Button(row, text=f"{label}  OFF",
                            command=toggle_cmd,
                            bg="#374151", fg=self.C_TXT,
                            font=self.FONT_S, relief=tk.FLAT,
                            padx=6, pady=4, width=20, anchor='w')
            btn.pack(side=tk.LEFT, padx=(0,4))
            tk.Label(row, text="→", bg=self.C_CARD,
                     fg=self.C_DIM, font=self.FONT_S).pack(side=tk.LEFT)
            var = tk.StringVar(value=default)
            tk.Entry(row, textvariable=var, width=5,
                     bg="#3a3a50", fg=self.C_TXT, insertbackground=self.C_TXT,
                     font=self.FONT_S, bd=0).pack(side=tk.LEFT, padx=3)
            tk.Label(row, text="%", bg=self.C_CARD, fg=self.C_DIM,
                     font=self.FONT_S).pack(side=tk.LEFT)
            tk.Button(row, text="Auto", command=auto_cmd,
                      bg=self.C_ACNT, fg="black", font=self.FONT_S,
                      relief=tk.FLAT, padx=4).pack(side=tk.LEFT, padx=4)
            setattr(self, btn_attr, btn)
            setattr(self, target_attr, var)

        _pump_row("Llenado CH3+CH4", 'btn_bniv', 'fill_target', "85.0",
                  self._toggle_bomba_nivel, self._autostop_nivel)
        _pump_row("Vaciado1 CH8",    'btn_vac1', 'vac1_target', "20.0",
                  self._toggle_vac1, self._autostop_vac1)
        _pump_row("Vaciado2 CH10",   'btn_vac2', 'vac2_target', "5.0",
                  self._toggle_vac2, self._autostop_vac2)

        # Burbujeo CH2
        row_burb = tk.Frame(f, bg=self.C_CARD)
        row_burb.pack(fill=tk.X, pady=3)
        self.btn_burb = tk.Button(row_burb, text="Burbujeo (CH2)  OFF",
                                  command=self._toggle_burbujeo,
                                  bg="#374151", fg=self.C_TXT,
                                  font=self.FONT_S, relief=tk.FLAT,
                                  padx=6, pady=4, width=22, anchor='w')
        self.btn_burb.pack(side=tk.LEFT)

        # Tira LED CH5
        row_led = tk.Frame(f, bg=self.C_CARD)
        row_led.pack(fill=tk.X, pady=3)
        self.btn_led = tk.Button(row_led, text="Tira LED   (CH5)  OFF",
                                 command=self._toggle_led,
                                 bg="#374151", fg=self.C_TXT,
                                 font=self.FONT_S, relief=tk.FLAT,
                                 padx=6, pady=4, width=22, anchor='w')
        self.btn_led.pack(side=tk.LEFT)

        self.lbl_fill = tk.Label(f, text="", bg=self.C_CARD,
                                 fg=self.C_DIM, font=self.FONT_S)
        self.lbl_fill.pack()

    # ── Card control difuso ───────────────────────────────────────────────────
    def _card_control(self, parent):
        f = self._card(parent, "Control difuso pH  (Ts = 30 s)")
        f.pack(fill=tk.BOTH, expand=False)

        # Métricas
        grid = tk.Frame(f, bg=self.C_CARD)
        grid.pack(fill=tk.X)
        metricas = [
            ("Error",     'lbl_err'),
            ("t_pulso",   'lbl_tp'),
            ("Próx. ciclo", 'lbl_cnt'),
        ]
        for col, (et, attr) in enumerate(metricas):
            tk.Label(grid, text=et, bg=self.C_CARD, fg=self.C_DIM,
                     font=self.FONT_S).grid(row=0, column=col, padx=12, sticky='w')
            lbl = tk.Label(grid, text="---", bg=self.C_CARD, fg=self.C_TXT,
                           font=("DejaVu Sans", 14, "bold"))
            lbl.grid(row=1, column=col, padx=12, sticky='w')
            setattr(self, attr, lbl)

        # Barra ciclo
        tk.Label(f, text="Progreso ciclo", bg=self.C_CARD, fg=self.C_DIM,
                 font=self.FONT_S).pack(anchor='w', pady=(8,1))
        self.canvas_ciclo = tk.Canvas(f, height=18, bg="#1e1e2e",
                                      highlightthickness=0)
        self.canvas_ciclo.pack(fill=tk.X, pady=(0,6))

        # Indicador bomba
        row = tk.Frame(f, bg=self.C_CARD)
        row.pack(pady=4)
        tk.Label(row, text="Bomba CH3+CH4:", bg=self.C_CARD, fg=self.C_DIM,
                 font=self.FONT_N).pack(side=tk.LEFT, padx=4)
        self.ind_bomba = tk.Label(row, text="  OFF  ", bg="#374151",
                                  fg=self.C_TXT, font=("DejaVu Sans", 13, "bold"),
                                  relief=tk.FLAT, padx=8, pady=4)
        self.ind_bomba.pack(side=tk.LEFT)

        # Botones de control
        btns = tk.Frame(f, bg=self.C_CARD)
        btns.pack(pady=8)
        self.btn_ctrl = tk.Button(btns, text="▶  Iniciar control pH",
                                  command=self._toggle_control,
                                  bg=self.C_GRN, fg="black",
                                  font=self.FONT_H, relief=tk.FLAT,
                                  padx=12, pady=7)
        self.btn_ctrl.pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="💾 Exportar CSV",
                  command=lambda: threading.Thread(target=guardar_csv, daemon=True).start(),
                  bg="#374151", fg=self.C_TXT, font=self.FONT_S,
                  relief=tk.FLAT, padx=8, pady=7).pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="🗑 Nueva prueba",
                  command=nueva_prueba,
                  bg="#7F1D1D", fg=self.C_TXT, font=self.FONT_S,
                  relief=tk.FLAT, padx=8, pady=7).pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="📈 Ver curvas",
                  command=self._ver_curvas,
                  bg="#374151", fg=self.C_TXT, font=self.FONT_S,
                  relief=tk.FLAT, padx=8, pady=7).pack(side=tk.LEFT, padx=4)

        tk.Button(btns, text="Pulso manual",
                  command=self._pulso_manual,
                  bg="#7B1FA2", fg="white",
                  font=self.FONT_N, relief=tk.FLAT,
                  padx=10, pady=7).pack(side=tk.LEFT, padx=4)

        # Slider de pulso manual
        row2 = tk.Frame(f, bg=self.C_CARD)
        row2.pack()
        tk.Label(row2, text="Duración pulso:", bg=self.C_CARD,
                 fg=self.C_DIM, font=self.FONT_S).pack(side=tk.LEFT)
        self.pulso_var = tk.IntVar(value=2)
        tk.Scale(row2, from_=1, to=7, orient=tk.HORIZONTAL,
                 variable=self.pulso_var, length=160,
                 bg=self.C_CARD, fg=self.C_TXT, troughcolor="#1e1e2e",
                 highlightthickness=0, font=self.FONT_S).pack(side=tk.LEFT, padx=6)
        tk.Label(row2, text="s", bg=self.C_CARD, fg=self.C_DIM,
                 font=self.FONT_S).pack(side=tk.LEFT)

    # ── Card log ───────────────────────────────────────────────────────────────
    def _card_log(self, parent):
        f = tk.LabelFrame(parent, text="Registro", bg=self.C_CARD,
                          fg=self.C_ACNT, font=self.FONT_H,
                          bd=2, relief=tk.GROOVE, padx=6, pady=6)
        f.pack(fill=tk.BOTH, expand=True, pady=4)
        self.txt_log = tk.Text(f, bg="#0f0f1a", fg=self.C_TXT,
                               font=self.FONT_M, state=tk.DISABLED,
                               bd=0, wrap=tk.WORD, height=10)
        sc = ttk.Scrollbar(f, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sc.set)
        sc.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # ── Acciones de botones ────────────────────────────────────────────────────
    def _toggle_control(self):
        with _lock:
            state['control_on'] = not state['control_on']
            on = state['control_on']
        if on:
            state['ciclo_cnt'] = 0
            _add_log("▶ Control pH INICIADO")
        else:
            _add_log("⏸ Control pH DETENIDO")

    def _toggle_bomba_nivel(self):
        # Llenado usa la bomba neutralizadora (CH3+CH4)
        with _lock:
            on = not state['bomba_neut']
            state['bomba_neut'] = on
        if on: neutraliz_on()
        else:  neutraliz_off()
        _add_log(f"Bomba Llenado CH3+CH4 {'ON' if on else 'OFF'}")

    # ── Calibración ───────────────────────────────────────────────────────────
    def _cal_actualizar_label(self):
        self.lbl_cal.config(
            text=f"Vacío: {_cal_vacio:.0f} mm   Lleno: {_cal_lleno:.0f} mm")

    def _cal_marcar_vacio(self):
        global _cal_vacio
        with _lock:
            d = state['dist_raw']
        if d is None:
            _add_log("⚠ Sin lectura del sensor")
            return
        if d < 700:
            _add_log(f"⚠ Lectura {d:.0f} mm — parece reflexión espuria, espera a que el sensor estabilice")
            return
        _cal_vacio = d
        _nivel_cal_ok[0] = True
        _dist_buf.clear()
        _last_nivel_valid[0] = None
        self._cal_actualizar_label()
        _add_log(f"📍 VACÍO marcado: {d:.0f} mm — nivel activo cuando ≥ {_NIVEL_MIN_CONFIABLE:.0f}%")

    def _cal_marcar_lleno(self):
        global _cal_lleno
        with _lock:
            d = state['dist_raw']
        if d is None:
            _add_log("⚠ Sin lectura del sensor")
            return
        _cal_lleno = d
        _dist_buf.clear()
        self._cal_actualizar_label()
        _add_log(f"📍 LLENO marcado: {d:.0f} mm")

    def _cal_guardar(self):
        _guardar_cal()
        _add_log(f"💾 Calibración guardada: vacío={_cal_vacio:.0f} lleno={_cal_lleno:.0f}")

    # ── Curvas de respuesta ────────────────────────────────────────────────────
    def _ver_curvas(self):
        with _lock:
            recs = list(state['registros'])
        if len(recs) < 2:
            _add_log("⚠ Sin datos suficientes para graficar (mínimo 2 ciclos)")
            return
        threading.Thread(target=self._graficar, args=(recs,), daemon=True).start()

    def _graficar(self, recs):
        try:
            import matplotlib
            matplotlib.use('TkAgg')
            import matplotlib.pyplot as plt
            import matplotlib.gridspec as gridspec
            from matplotlib.patches import Patch
            from datetime import datetime as _dt
        except ImportError:
            _add_log("⚠ Instala matplotlib:  pip install matplotlib --break-system-packages")
            return

        # Tiempo relativo en segundos
        t0 = _dt.fromisoformat(recs[0]['ts'])
        ts = [(_dt.fromisoformat(r['ts']) - t0).total_seconds() for r in recs]
        ph  = [r['ph']      for r in recs]
        sp  = [r['sp']      for r in recs]
        err = [r['error']   for r in recs]
        tp  = [r['t_pulso'] for r in recs]
        acc = [r['accion']  for r in recs]

        def bar_color(a):
            if a.startswith('pulso_'): return '#3B82F6'
            if a == 'guarda_nivel':    return '#F59E0B'
            return '#6B7280'

        fig = plt.figure(figsize=(13, 8), facecolor='#111827')
        gs  = gridspec.GridSpec(3, 1, hspace=0.45, figure=fig,
                                left=0.07, right=0.97, top=0.90, bottom=0.08)
        axes = [fig.add_subplot(gs[i]) for i in range(3)]
        for ax in axes:
            ax.set_facecolor('#1F2937')
            ax.tick_params(colors='#9CA3AF', labelsize=9)
            ax.spines[:].set_color('#374151')
            ax.grid(True, color='#374151', linewidth=0.5, linestyle='--')
            ax.yaxis.label.set_color('#D1D5DB')
            ax.xaxis.label.set_color('#D1D5DB')
            ax.title.set_color('#F9FAFB')

        # Subplot 1 — pH
        axes[0].plot(ts, ph, color='#34D399', lw=2, label='pH medido')
        axes[0].step(ts, sp, color='#F87171', lw=1.5, linestyle='--',
                     where='post', label='Setpoint')
        axes[0].axhspan(sp[0]-0.3, sp[0]+0.3, color='#F87171', alpha=0.07, label='±0.3')
        axes[0].set_ylabel('pH')
        axes[0].set_title('Respuesta pH — Fuzzy Mamdani')
        axes[0].legend(loc='lower right', facecolor='#374151',
                       edgecolor='none', labelcolor='#D1D5DB', fontsize=8)

        # Subplot 2 — Error
        axes[1].plot(ts, err, color='#FB923C', lw=1.8)
        axes[1].axhline(0,    color='#6B7280', lw=0.8)
        axes[1].axhline( 0.2, color='#6B7280', lw=0.8, linestyle=':')
        axes[1].axhline(-0.2, color='#6B7280', lw=0.8, linestyle=':')
        axes[1].fill_between(ts, -0.2, 0.2, color='#6B7280', alpha=0.10)
        axes[1].set_ylabel('Error (pH)')

        # Subplot 3 — t_pulso
        ancho = max(0.5, (ts[-1] - ts[0]) / max(len(ts), 1) * 0.8) if len(ts) > 1 else 2.0
        axes[2].bar(ts, tp, width=ancho, color=[bar_color(a) for a in acc])
        axes[2].set_ylim(0, 11)
        axes[2].set_ylabel('t_pulso (s)')
        axes[2].set_xlabel('Tiempo (s)')
        axes[2].legend(handles=[
            Patch(color='#3B82F6', label='Pulso bomba'),
            Patch(color='#6B7280', label='Banda muerta'),
            Patch(color='#F59E0B', label='Inhibido nivel'),
        ], loc='upper right', facecolor='#374151', edgecolor='none',
           labelcolor='#D1D5DB', fontsize=8)

        # Métricas rápidas
        e_est = sum(err[-max(1, len(err)//5):]) / max(1, len(err)//5)
        ovs   = max(0.0, max(ph) - sp[0])
        fig.text(0.5, 0.935,
                 f"SP={sp[0]:.2f}  pH₀={ph[0]:.3f}  pH_f={ph[-1]:.3f}  "
                 f"Sobreimpulso={ovs:.3f}  e_est={e_est:+.3f}  "
                 f"Pulsos={sum(1 for a in acc if a.startswith('pulso_'))}  "
                 f"Duración={ts[-1]:.0f}s",
                 ha='center', fontsize=8.5, color='#9CA3AF', fontfamily='monospace')

        # Guardar PNG junto al CSV más reciente (si existe)
        import glob as _glob
        csvs = sorted(_glob.glob(os.path.join(_CSV_DIR, '*.csv')))
        if csvs:
            out = csvs[-1].replace('.csv', '_respuesta.png')
            try:
                plt.savefig(out, dpi=150, facecolor='#111827')
                _add_log(f"📊 PNG guardado: {out}")
            except Exception:
                pass

        plt.show()

    def _cal_aplicar_manual(self):
        global _cal_vacio, _cal_lleno
        try:
            v = self._cal_v_var.get().strip()
            l = self._cal_l_var.get().strip()
            if v: _cal_vacio = float(v)
            if l: _cal_lleno = float(l)
            _nivel_cal_ok[0] = True   # calibración manual = confiable
            _dist_buf.clear()
            _last_nivel_valid[0] = None
            self._cal_actualizar_label()
            _add_log(f"✓ Cal. manual: vacío={_cal_vacio:.0f} lleno={_cal_lleno:.0f} — nivel habilitado")
        except ValueError:
            _add_log("⚠ Valores de calibración inválidos")

    def _toggle_burbujeo(self):
        with _lock:
            state['burbujeo'] = not state['burbujeo']
            on = state['burbujeo']
        burbujeo(on)
        _add_log(f"Burbujeo CH2 {'ON' if on else 'OFF'}")

    def _toggle_led(self):
        with _lock:
            state['led'] = not state['led']
            on = state['led']
        tira_led(on)
        _add_log(f"Tira LED CH5 {'ON' if on else 'OFF'}")

    def _toggle_vac1(self):
        with _lock:
            state['bomba_vac1'] = not state['bomba_vac1']
            on = state['bomba_vac1']
        bomba_vac1(on)
        _add_log(f"BombaVaciado1 CH8 {'ON' if on else 'OFF'}")

    def _toggle_vac2(self):
        with _lock:
            state['bomba_vac2'] = not state['bomba_vac2']
            on = state['bomba_vac2']
        bomba_vac2(on)
        _add_log(f"BombaVaciado2 CH10 {'ON' if on else 'OFF'}")

    def _autostop_nivel(self):
        try: target = float(self.fill_target.get())
        except ValueError: return
        def _watch():
            neutraliz_on()
            with _lock: state['bomba_neut'] = True
            deadline = time.time() + MAX_FILL_MIN * 60
            _add_log(f"Auto-llenado CH3+CH4 → {target:.1f}%  (máx {MAX_FILL_MIN} min)")
            confirm = 0
            while not _stop.is_set():
                if time.time() > deadline:
                    neutraliz_off()
                    with _lock: state['bomba_neut'] = False
                    _add_log(f"⚠ TIMEOUT llenado ({MAX_FILL_MIN} min) — bomba detenida")
                    break
                n, _ = leer_nivel()
                if n is not None and n >= target:
                    confirm += 1
                    _add_log(f"  Nivel {n:.1f}% ≥ {target:.1f}% ({confirm}/{CONFIRM_READS})")
                    if confirm >= CONFIRM_READS:
                        neutraliz_off()
                        with _lock: state['bomba_neut'] = False
                        _add_log(f"✅ Auto-llenado confirmado: {n:.1f}%")
                        break
                else:
                    confirm = 0
                time.sleep(3)
        threading.Thread(target=_watch, daemon=True).start()

    def _autostop_vac1(self):
        try: target = float(self.vac1_target.get())
        except ValueError: return
        def _watch():
            bomba_vac1(True)
            with _lock: state['bomba_vac1'] = True
            deadline = time.time() + MAX_FILL_MIN * 60
            _add_log(f"Auto-vaciado1 CH8 → {target:.1f}%  (máx {MAX_FILL_MIN} min)")
            confirm = 0
            while not _stop.is_set():
                if time.time() > deadline:
                    bomba_vac1(False)
                    with _lock: state['bomba_vac1'] = False
                    _add_log(f"⚠ TIMEOUT vaciado1 — bomba detenida")
                    break
                n, _ = leer_nivel()
                if n is not None and n <= target:
                    confirm += 1
                    if confirm >= CONFIRM_READS:
                        bomba_vac1(False)
                        with _lock: state['bomba_vac1'] = False
                        _add_log(f"✅ Auto-vaciado1 confirmado: {n:.1f}%")
                        break
                else:
                    confirm = 0
                time.sleep(3)
        threading.Thread(target=_watch, daemon=True).start()

    def _autostop_vac2(self):
        try: target = float(self.vac2_target.get())
        except ValueError: return
        def _watch():
            bomba_vac2(True)
            with _lock: state['bomba_vac2'] = True
            deadline = time.time() + MAX_FILL_MIN * 60
            _add_log(f"Auto-vaciado2 CH10 → {target:.1f}%  (máx {MAX_FILL_MIN} min)")
            confirm = 0
            while not _stop.is_set():
                if time.time() > deadline:
                    bomba_vac2(False)
                    with _lock: state['bomba_vac2'] = False
                    _add_log(f"⚠ TIMEOUT vaciado2 — bomba detenida")
                    break
                n, _ = leer_nivel()
                if n is not None and n <= target:
                    confirm += 1
                    if confirm >= CONFIRM_READS:
                        bomba_vac2(False)
                        with _lock: state['bomba_vac2'] = False
                        _add_log(f"✅ Auto-vaciado2 confirmado: {n:.1f}%")
                        break
                else:
                    confirm = 0
                time.sleep(3)
        threading.Thread(target=_watch, daemon=True).start()

    def _pulso_manual(self):
        seg = self.pulso_var.get()
        _add_log(f"Pulso manual {seg} s → Bomba ON")
        def _run():
            neutraliz_on()
            with _lock: state['bomba_neut'] = True
            time.sleep(seg)
            neutraliz_off()
            with _lock: state['bomba_neut'] = False
            _add_log("Pulso manual → Bomba OFF")
        threading.Thread(target=_run, daemon=True).start()

    # ── Tick de actualización UI (cada 500 ms) ────────────────────────────────
    def _tick(self):
        with _lock:
            ph     = state['ph']
            temp   = state['temp']
            nivel  = state['nivel']
            dist_r = state['dist_raw']
            sp     = state['sp']
            error  = state['error']
            tp     = state['t_pulso']
            cnt    = state['ciclo_cnt']
            ctrl   = state['control_on']
            bneut  = state['bomba_neut']
            bled   = state['led']
            bburb  = state['burbujeo']
            spike  = state['sensor_spike']
            bvac1  = state['bomba_vac1']
            bvac2  = state['bomba_vac2']
            n_max  = state['nivel_max']
            n_hist = state['nivel_hist']
            ts_s   = int(state['ts_s'])
            log    = list(state['log'])

        # Sensores
        self.lbl_ph.config(
            text=f"{ph:.3f}" if ph else "---",
            fg=self.C_GRN if ph and abs(sp-ph) < 0.3 else
               self.C_YLW if ph and abs(sp-ph) < 1.0 else self.C_RED)
        self.lbl_niv.config(
            text=f"{nivel:.1f} %" if nivel is not None else "---",
            fg=self.C_RED if nivel and nivel >= n_max else
               self.C_YLW if nivel and nivel >= n_hist else self.C_GRN)
        self.lbl_dist.config(
            text=("⚡ " if spike else "") + (f"{dist_r:.0f} mm" if dist_r is not None else "---"),
            fg=self.C_RED if spike else self.C_DIM)
        self.lbl_tmp.config(
            text=f"{temp:.1f} °C" if temp else "---")

        # Barra de nivel
        self._draw_barra(self.canvas_nivel, nivel, n_max, n_hist)

        # Métricas control
        self.lbl_err.config(
            text=f"{error:+.3f}" if error is not None else "---",
            fg=self.C_GRN if error is not None and error <= 0 else self.C_YLW)
        self.lbl_tp.config(
            text=f"{tp:.2f} s" if tp is not None else "---")
        self.lbl_cnt.config(
            text=f"{max(0, ts_s - cnt)} s")

        # Barra ciclo
        w = self.canvas_ciclo.winfo_width()
        self.canvas_ciclo.delete("all")
        if w > 1:
            frac = cnt / max(1, ts_s)
            self.canvas_ciclo.create_rectangle(0, 0, int(w*frac), 18,
                fill=self.C_ACNT, outline="")

        # Indicador bomba
        if bneut:
            self.ind_bomba.config(text="  ON  ", bg=self.C_GRN, fg="black")
        else:
            self.ind_bomba.config(text="  OFF  ", bg="#374151", fg=self.C_DIM)

        # Botón control
        if ctrl:
            self.btn_ctrl.config(text="⏸  Detener control pH",
                                 bg=self.C_RED, fg="white")
        else:
            self.btn_ctrl.config(text="▶  Iniciar control pH",
                                 bg=self.C_GRN, fg="black")

        # Botones de bombas de preparación
        for on, btn, label in [
            (bneut, self.btn_bniv, "Llenado CH3+CH4"),
            (bvac1, self.btn_vac1, "Vaciado1 CH8"),
            (bvac2, self.btn_vac2, "Vaciado2 CH10"),
        ]:
            if on:
                btn.config(text=f"{label}  ██ ON",  bg=self.C_GRN, fg="black")
            else:
                btn.config(text=f"{label}  ░░ OFF", bg="#374151", fg=self.C_TXT)

        # Burbujeo
        if bburb:
            self.btn_burb.config(text="Burbujeo (CH2)  ██ ON",  bg="#06b6d4", fg="black")
        else:
            self.btn_burb.config(text="Burbujeo (CH2)  ░░ OFF", bg="#374151", fg=self.C_TXT)

        # Tira LED
        if bled:
            self.btn_led.config(text="Tira LED (CH5)  ██ ON",  bg="#f59e0b", fg="black")
        else:
            self.btn_led.config(text="Tira LED (CH5)  ░░ OFF", bg="#374151", fg=self.C_TXT)

        # Estado
        if not ser:
            self.lbl_estado.config(text="● SIN SERIAL", fg=self.C_RED)
        elif ph is not None:
            self.lbl_estado.config(text="● CONECTADO", fg=self.C_GRN)
        else:
            self.lbl_estado.config(text="● ESPERANDO", fg=self.C_YLW)

        # Log
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.insert(tk.END, "\n".join(log[-50:]))
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

        self.root.after(500, self._tick)

    def _draw_barra(self, canvas, nivel, n_max, n_hist):
        canvas.delete("all")
        w = canvas.winfo_width()
        if w <= 1: return
        h = 22
        # Fondo
        canvas.create_rectangle(0, 0, w, h, fill="#1e1e2e", outline="")
        # Relleno nivel
        if nivel is not None:
            color = self.C_RED  if nivel >= n_max  else \
                    self.C_YLW  if nivel >= n_hist else self.C_ACNT
            canvas.create_rectangle(0, 0, int(w * nivel/100), h,
                                    fill=color, outline="")
        # Líneas de umbral
        canvas.create_line(int(w * n_max/100), 0,
                           int(w * n_max/100), h, fill=self.C_RED, width=2)
        canvas.create_line(int(w * n_hist/100), 0,
                           int(w * n_hist/100), h, fill=self.C_YLW, width=2)
        # Texto
        txt = f"{nivel:.1f}%" if nivel is not None else "---"
        canvas.create_text(w//2, h//2, text=txt, fill="white",
                          font=("DejaVu Sans", 9, "bold"))

# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def _on_close(root):
    if messagebox.askokcancel("Salir", "¿Guardar CSV y salir?"):
        _limpiar()
        root.destroy()

signal.signal(signal.SIGINT,  lambda s, f: (_limpiar(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda s, f: (_limpiar(), sys.exit(0)))

root = tk.Tk()
app  = App(root)
root.protocol("WM_DELETE_WINDOW", lambda: _on_close(root))
root.mainloop()
_limpiar()
