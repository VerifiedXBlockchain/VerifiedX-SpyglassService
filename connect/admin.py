from django.contrib import admin

from .models import ChatThread, ChatMessage


@admin.register(ChatThread)
class ChatThreadAdmin(admin.ModelAdmin):
    readonly_fields = ["uuid"]


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    pass
