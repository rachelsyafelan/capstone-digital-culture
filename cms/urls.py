from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('api/recommended/', views.recommended_content_api, name='recommended_content_api'),
    path('explore/', views.explore, name='explore'),
    path('event_detail/<int:id>/', views.event_detail, name='event_detail'),
    path('content/<int:id>/', views.event_detail, name='content_detail'),
    path('feedback/', views.feedback, name='feedback'),
    path('save-feedback/', views.save_feedback, name='save_feedback'),
    path('chat-ai/', views.chat_with_ai, name='chat_with_ai'),
    path('culture_performance/', views.culture_performance, name='culture_performance'),
    path('business_performance/', views.business_performance, name='business_performance'),
    path('track/', views.track_activity, name='track_activity'),
    path('event/<int:id>/register/', views.register_event, name='register_event'),
    path('event/<int:id>/check-status/', views.check_participation_status, name='check_participation_status'),
    path('success/', views.success_page, name='success_page'),
    path('news_redirect/<int:id>/', views.redirect_news, name='redirect_news'),
    path('attribute_redirect/<int:id>/', views.redirect_attribute, name='redirect_attribute'),

    # Admin auth
    path('admin-login/', views.admin_login, name='admin_login'),
    path('admin-forgot-password/', views.admin_forgot_password, name='admin_forgot_password'),
    path('admin-reset-password/<str:uidb64>/<str:token>/', views.admin_reset_password, name='admin_reset_password'),
    path('admin-logout/', views.admin_logout_view, name='admin_logout'),

    # Admin pages
    path('admin-home/', views.admin_home, name='admin_home'),
    path('admin-explore/', views.explore_admin, name='admin_explore'),
    path('admin-explore/add/', views.admin_add_event, name='admin_add_event'),
    path('admin-explore/edit/<int:id>/', views.admin_edit_event, name='admin_edit_event'),
    path('admin-explore/event-preview/<int:id>/', views.admin_event_preview, name='admin_event_preview'),
    path('admin-explore/delete/<int:id>/', views.admin_delete_event, name='admin_delete_event'),
    path('admin-explore/status/<int:id>/', views.admin_change_event_status, name='admin_change_event_status'),
    path('admin-explore/news/add/', views.admin_add_news, name='admin_add_news'),
    path('admin-explore/news/edit/<int:id>/', views.admin_edit_news, name='admin_edit_news'),
    path('admin-explore/news/delete/<int:id>/', views.admin_delete_news, name='admin_delete_news'),
    path('admin-explore/attribute/add/', views.admin_add_attribute, name='admin_add_attribute'),
    path('admin-explore/attribute/edit/<int:id>/', views.admin_edit_attribute, name='admin_edit_attribute'),
    path('admin-explore/attribute/delete/<int:id>/', views.admin_delete_attribute, name='admin_delete_attribute'),
    path('admin-feedback/', views.feedback_admin, name='admin_feedback'),

    # Attendance (public submit + admin management)
    path('event/<int:id>/attendance/', views.submit_attendance, name='submit_attendance'),
    path('admin-attendance/', views.admin_attendance_list, name='admin_attendance'),
    path('admin-attendance/verify/<int:attendance_id>/', views.admin_verify_attendance, name='admin_verify_attendance'),
    path('admin-attendance/reject/<int:attendance_id>/', views.admin_reject_attendance, name='admin_reject_attendance'),

]