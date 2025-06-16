import sounddevice as sd
import soundfile as sf
import numpy as np
import time
import os
import subprocess
import webbrowser
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    pycaw_available = True
except ImportError:
    pycaw_available = False

# Muestra todos los dispositivos de audio disponibles de forma más legible
def list_audio_devices():
    devices = sd.query_devices()
    print("\n=== DISPOSITIVOS DE AUDIO DISPONIBLES ===")
    print("DISPOSITIVOS DE ENTRADA (GRABACIÓN):")
    for i, device in enumerate(devices):
        if device['max_input_channels'] > 0:
            print(f"  [{i}] {device['name']} (Canales: {device['max_input_channels']})")
    
    print("\nDISPOSITIVOS DE SALIDA (REPRODUCCIÓN):")
    for i, device in enumerate(devices):
        if device['max_output_channels'] > 0:
            print(f"  [{i}] {device['name']} (Canales: {device['max_output_channels']})")
    
    print("\nDispositivos predeterminados:")
    print(f"  Entrada predeterminada: {sd.query_devices(kind='input')['name']}")
    print(f"  Salida predeterminada: {sd.query_devices(kind='output')['name']}")

# Busca un dispositivo por nombre (parcial)
def find_device_by_name(name_fragment):
    devices = sd.query_devices()
    for idx, device in enumerate(devices):
        if name_fragment.lower() in device['name'].lower():
            return idx
    return None

# Verifica la configuración de Virtual Cable
def check_virtual_cable_setup():
    devices = sd.query_devices()
    cable_input = None
    cable_output = None
    
    for i, device in enumerate(devices):
        name = device['name'].lower()
        if 'cable input' in name:
            cable_input = i
        elif 'cable output' in name:
            cable_output = i
    
    print("\n=== DIAGNÓSTICO DE VIRTUAL CABLE ===")
    if cable_input is None and cable_output is None:
        print("❌ ERROR: No se encontraron dispositivos de VB-CABLE.")
        print("   Por favor, instala VB-CABLE desde: https://vb-audio.com/Cable/")
        if input("¿Quieres abrir la página de descarga de VB-CABLE? (s/n): ").lower() == 's':
            webbrowser.open("https://vb-audio.com/Cable/")
        return False
    
    if cable_input is None:
        print("❌ ERROR: Se encontró CABLE Output pero no CABLE Input.")
        print("   La instalación de VB-CABLE parece estar incompleta.")
        return False
    
    if cable_output is None:
        print("❌ ERROR: Se encontró CABLE Input pero no CABLE Output.")
        print("   La instalación de VB-CABLE parece estar incompleta.")
        return False
    
    print(f"✓ VB-CABLE encontrado: CABLE Input (dispositivo {cable_input}) y CABLE Output (dispositivo {cable_output})")
    
    # Verificar si CABLE Input está configurado como dispositivo de salida en Windows
    print("\nPara que la grabación funcione correctamente:")
    print("1. Abre la configuración de sonido de Windows")
    print("2. Configura 'CABLE Input' como dispositivo de salida predeterminado")
    print("3. Reproduce audio mientras grabas con este script")
    
    if input("\n¿Quieres abrir la configuración de sonido de Windows ahora? (s/n): ").lower() == 's':
        try:
            subprocess.run("start ms-settings:sound", shell=True)
        except:
            print("No se pudo abrir la configuración automáticamente.")
            print("Por favor, abre manualmente: Panel de control > Sonido > Reproducción")
    
    return True

# Verifica en tiempo real si hay audio pasando por el sistema
def monitor_audio_levels(device_index, duration=3, samplerate=44100):
    print(f"Monitorizando niveles de audio en dispositivo {device_index} durante {duration} segundos...")
    
    # Crear un callback que monitoree los niveles de audio
    levels = []
    
    def callback(indata, frames, time, status):
        if status:
            print(f"Estado: {status}")
        volume_norm = np.linalg.norm(indata) / np.sqrt(frames)
        levels.append(volume_norm)
        print(f"Nivel: {volume_norm:.6f}", end='\r')
    
    try:
        with sd.InputStream(device=device_index, channels=2, callback=callback,
                          blocksize=int(samplerate * 0.1),
                          samplerate=samplerate):
            sd.sleep(duration * 1000)
    except Exception as e:
        print(f"\nError al monitorizar audio: {e}")
        return False
    
    max_level = max(levels) if levels else 0
    print(f"\nNivel máximo detectado: {max_level:.6f}")
    
    if max_level < 0.01:
        print("❌ No se detectó audio. Posibles problemas:")
        print("   - No se está reproduciendo audio en el sistema")
        print("   - CABLE Input no está configurado como dispositivo de salida")
        print("   - El volumen del sistema está muy bajo o silenciado")
        return False
    else:
        print("✓ Se detectó audio pasando por el dispositivo")
        return True

# Graba audio desde el dispositivo CABLE Output
def record_virtual_audio(filename, duration=5, samplerate=48000, channels=2):
    try:
        # Verificar la configuración de Virtual Cable
        if not check_virtual_cable_setup():
            return False
        
        # Buscar dispositivo CABLE Output para grabación
        device_index = find_device_by_name('cable output')
        if device_index is None:
            device_index = find_device_by_name('cable')
            if device_index is None:
                print("❌ No se pudo encontrar el dispositivo Virtual Cable.")
                return False
        
        device_name = sd.query_devices(device_index)['name']
        print(f"\n=== CONFIGURACIÓN DE GRABACIÓN ===")
        print(f"Dispositivo: '{device_name}' (índice {device_index})")
        print(f"Frecuencia de muestreo: {samplerate} Hz")
        print(f"Canales: {channels}")
        
        # Monitorizar si hay audio pasando por el dispositivo antes de grabar
        print("\n=== COMPROBACIÓN DE AUDIO ===")
        print("Asegúrate de estar reproduciendo audio en el sistema...")
        if not monitor_audio_levels(device_index, duration=3):
            if input("\n¿Continuar con la grabación de todos modos? (s/n): ").lower() != 's':
                return False
        
        print(f"\n=== GRABANDO AUDIO ===")
        print(f"Grabando {duration} segundos desde '{device_name}'...")
        
        # Grabar audio
        audio = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=channels, device=device_index)
        
        # Mostrar un temporizador durante la grabación
        for remaining in range(duration, 0, -1):
            print(f"Grabando... {remaining} segundos restantes", end='\r')
            time.sleep(1)
        print("\nProcesando grabación...                ")
        
        sd.wait()  # Esperar a que termine la grabación
        
        # Aplicar reducción de ruido simple
        umbral = 0.01  # Umbral más bajo para capturar señales más débiles
        audio[np.abs(audio) < umbral] = 0
        
        # Normalizar solo si hay señal significativa
        max_amp = np.max(np.abs(audio))
        print(f"Nivel máximo en la grabación: {max_amp:.6f}")
        
        if max_amp > umbral:
            audio = audio / max_amp * 0.9
            print("✓ Audio normalizado correctamente")
        else:
            print("⚠ La grabación contiene niveles muy bajos o silencio")
        
        sf.write(filename, audio, samplerate)
        print(f"✓ Audio guardado en {filename}")
        
        return verificar_audio(filename)
    
    except Exception as e:
        print(f"❌ Error durante la grabación: {e}")
        return False

# Reproduce un archivo de audio
def reproducir_audio(filename):
    try:
        # Usar el dispositivo de salida predeterminado
        data, samplerate = sf.read(filename)
        print(f"Reproduciendo {filename} a través de {sd.query_devices(kind='output')['name']}...")
        sd.play(data, samplerate)
        sd.wait()
        print("Reproducción finalizada.")
        return True
    except Exception as e:
        print(f"Error al reproducir el audio: {e}")
        return False

# Comprueba si hay audio en el archivo (no solo silencio)
def verificar_audio(filename):
    try:
        data, _ = sf.read(filename)
        max_amplitude = np.max(np.abs(data))
        print(f"Nivel máximo de audio: {max_amplitude:.6f}")
        
        if max_amplitude < 0.01:
            print("ADVERTENCIA: El archivo parece contener solo silencio o audio muy bajo.")
            print("Asegúrate de que:")
            print("1. El dispositivo 'CABLE Input' está configurado como salida predeterminada en Windows")
            print("2. Estás reproduciendo algún sonido mientras grabas")
            print("3. El volumen del sistema está activado y no está silenciado")
            return False
        else:
            print("El archivo contiene audio (no solo silencio).")
            return True
    except Exception as e:
        print(f"Error al verificar el audio: {e}")
        return False

# Método alternativo de grabación usando otro dispositivo
def record_fallback(filename, duration=5, samplerate=48000):
    print("\n=== GRABACIÓN ALTERNATIVA ===")
    print("Intentando grabar con método alternativo...")
    
    # Listar dispositivos de entrada disponibles
    devices = sd.query_devices()
    input_devices = [(i, d) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    
    if not input_devices:
        print("❌ No se encontraron dispositivos de entrada disponibles.")
        return False
    
    print("\nDispositivos de entrada disponibles:")
    for i, (idx, device) in enumerate(input_devices):
        print(f"  [{i}] {device['name']} (Canales: {device['max_input_channels']})")
    
    try:
        choice = int(input("\nSelecciona un dispositivo para grabar [0]: ") or "0")
        if choice < 0 or choice >= len(input_devices):
            print("Selección inválida, usando la primera opción.")
            choice = 0
    except ValueError:
        print("Entrada inválida, usando la primera opción.")
        choice = 0
    
    device_idx = input_devices[choice][0]
    device_name = input_devices[choice][1]['name']
    
    print(f"Grabando {duration} segundos desde '{device_name}'...")
    
    try:
        # Grabar audio
        audio = sd.rec(int(duration * samplerate), samplerate=samplerate, 
                      channels=min(2, input_devices[choice][1]['max_input_channels']), 
                      device=device_idx)
        
        # Mostrar un temporizador durante la grabación
        for remaining in range(duration, 0, -1):
            print(f"Grabando... {remaining} segundos restantes", end='\r')
            time.sleep(1)
        print("\nProcesando grabación...                ")
        
        sd.wait()  # Esperar a que termine la grabación
        
        # Normalizar si es necesario
        if np.max(np.abs(audio)) > 0.01:
            audio = audio / np.max(np.abs(audio)) * 0.9
        
        sf.write(filename, audio, samplerate)
        print(f"✓ Audio guardado en {filename}")
        return True
    
    except Exception as e:
        print(f"❌ Error durante la grabación alternativa: {e}")
        return False

if __name__ == "__main__":
    # Mostrar todos los dispositivos disponibles
    list_audio_devices()
    
    output_filename = "virtual_audio.wav"
    
    # Grabar audio del sistema
    if not record_virtual_audio(output_filename, duration=5):
        print("\nLa grabación con Virtual Cable no funcionó correctamente.")
        if input("¿Quieres intentar grabar con otro dispositivo? (s/n): ").lower() == 's':
            record_fallback(output_filename, duration=5)
    
    # Verificar y reproducir si existe el archivo
    if os.path.exists(output_filename):
        verificar_audio(output_filename)
        reproducir_audio(output_filename)