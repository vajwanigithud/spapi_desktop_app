"""Routes package initializer."""

from .printer_routes import register_printer_routes
from .barcode_print_routes import register_barcode_print_routes
from .printer_health_routes import register_printer_health_routes
from .print_log_routes import register_print_log_routes

__all__ = [
    "register_printer_routes",
    "register_barcode_print_routes",
    "register_printer_health_routes",
    "register_print_log_routes",
]
