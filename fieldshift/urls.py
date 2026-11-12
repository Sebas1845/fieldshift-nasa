from django.contrib import admin
from django.urls import path
from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home, name='home'),
    path('api/analizar-terreno/', views.analizar_terreno, name='analizar_terreno'),
    path('api/escanear-hoja/', views.escanear_hoja, name='escanear_hoja'),
]