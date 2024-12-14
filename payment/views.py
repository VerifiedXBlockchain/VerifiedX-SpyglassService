from django.shortcuts import render

from django.views.decorators.clickjacking import xframe_options_exempt


@xframe_options_exempt
def payment_embed(request):
    return render(request, "payment_embed.html")
