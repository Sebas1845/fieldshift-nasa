from django.db import models
from django.contrib.auth.models import User



class ComentarioProyecto(models.Model):
    MODULOS = [
        ('MAPA_NASA', 'Mapa Satelital y Datos NASA'),
        ('PLAN_SIEMBRA', 'Recomendación y Plan de Siembra'),
        ('ESCANER_HOJAS', 'Escáner Experto de Hojas'),
        ('GENERAL', 'Propuesta General GeoSync'),
    ]
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='comentarios')
    modulo = models.CharField(max_length=30, choices=MODULOS, default='GENERAL')
    calificacion = models.IntegerField(default=5)
    mensaje = models.TextField()
    fecha_creacion = models.DateTimeField(auto_now_add=True)


class ConsultaTerreno(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='consultas_terreno')
    latitud = models.FloatField()
    longitud = models.FloatField()
    datos_nasa_json = models.JSONField()
    cultivos_sugeridos = models.JSONField()
    generado_por_ia = models.BooleanField(default=False)
    fuente_motor = models.CharField(max_length=100, default='Programación Local')
    fecha_consulta = models.DateTimeField(auto_now_add=True)


class PlanSiembra(models.Model):
    consulta = models.ForeignKey(ConsultaTerreno, on_delete=models.CASCADE, related_name='planes_siembra')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    cultivo_elegido = models.CharField(max_length=100)
    etapas_json = models.JSONField()
    fecha_inicio = models.DateField(auto_now_add=True)
    activo = models.BooleanField(default=True)


class EscaneoPlanta(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='escaneos')
    imagen_hoja = models.ImageField(upload_to='hojas_escaneadas/')
    reconocida_en_sistema = models.BooleanField(default=True)
    planta_detectada = models.CharField(max_length=120, default='Planta no identificada')
    diagnostico_ia = models.TextField(blank=True, null=True)
    generado_por_ia = models.BooleanField(default=False)
    fuente_motor = models.CharField(max_length=100, default='Programación Local')
    fecha_escaneo = models.DateTimeField(auto_now_add=True)


class PlanRecuperacion(models.Model):
    escaneo = models.OneToOneField(EscaneoPlanta, on_delete=models.CASCADE, related_name='plan_recuperacion')
    severidad = models.CharField(max_length=50)
    pasos_recuperacion_json = models.JSONField()
    completado = models.BooleanField(default=False)

