from django.contrib import admin
from django.urls import path, re_path
from django.conf import settings
from django.views.static import serve
from core import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index_view, name='index'),
    path('acceso/', views.acceso_view, name='acceso'),
    path('registro/', views.registro_view, name='registro'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('api/historial/', views.historial_usuario_view, name='historial'),
    path('api/terreno/analizar/', views.analizar_terreno_nasa_view, name='analizar_terreno'),
    path('api/terreno/plan-siembra/', views.activar_plan_siembra_view, name='activar_plan_siembra'),
    path('api/planta/escanear/', views.escanear_hoja_experto_view, name='escanear_hoja'),
    path('api/comentarios/', views.comentarios_api_view, name='comentarios_api'),
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]