from django import dispatch


sale_started = dispatch.Signal()
sale_completed = dispatch.Signal()
