import sys
import io

from typing import BinaryIO, Any


from .._base_converter import DocumentConverter, DocumentConverterResult
from .._stream_info import StreamInfo
from .._exceptions import MissingDependencyException, MISSING_DEPENDENCY_MESSAGE


# Try loading optional (but in this case, required) dependencies
# Save reporting of any exceptions for later
_dependency_exc_info = None
try:
    import pdfminer
    import pdfminer.high_level
    import pdfminer.pdfpage
    import pdfminer.layout
    import pdfminer.converter
    import pdfminer.pdfinterp
    import PyPDF2
except ImportError:
    # Preserve the error and stack trace for later
    _dependency_exc_info = sys.exc_info()


ACCEPTED_MIME_TYPE_PREFIXES = [
    "application/pdf",
    "application/x-pdf",
]

ACCEPTED_FILE_EXTENSIONS = [".pdf"]


class PdfConverter(DocumentConverter):
    """
    Converts PDFs to Markdown. Most style information is ignored, so the results are essentially plain-text.
    Enhanced to capture fillable form fields and represent them inline where they naturally occur.
    """

    def accepts(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> bool:
        mimetype = (stream_info.mimetype or "").lower()
        extension = (stream_info.extension or "").lower()

        if extension in ACCEPTED_FILE_EXTENSIONS:
            return True

        for prefix in ACCEPTED_MIME_TYPE_PREFIXES:
            if mimetype.startswith(prefix):
                return True

        return False

    def _extract_text_by_pages(self, file_stream: BinaryIO) -> list:
        """Extract text content page by page"""
        pages_content = []
        
        try:
            file_stream.seek(0)
            
            # Extract text for each page
            from pdfminer.pdfpage import PDFPage
            from pdfminer.pdfinterp import PDFResourceManager, PDFPageInterpreter
            from pdfminer.converter import TextConverter
            from pdfminer.layout import LAParams
            
            resource_manager = PDFResourceManager()
            laparams = LAParams()
            
            for page_num, page in enumerate(PDFPage.get_pages(file_stream), 1):
                output_string = io.StringIO()
                device = TextConverter(resource_manager, output_string, laparams=laparams)
                interpreter = PDFPageInterpreter(resource_manager, device)
                interpreter.process_page(page)
                
                page_text = output_string.getvalue()
                device.close()
                output_string.close()
                
                pages_content.append({
                    'page_num': page_num,
                    'text': page_text
                })
                
        except Exception as e:
            # Fallback to simple text extraction
            file_stream.seek(0)
            text = pdfminer.high_level.extract_text(file_stream)
            pages_content = [{'page_num': 1, 'text': text}]
        
        return pages_content

    def _extract_form_fields_for_page(self, file_stream: BinaryIO, page_number: int) -> list:
        """Extract form fields that exist on a specific page"""
        page_fields = []
        
        try:
            file_stream.seek(0)
            reader = PyPDF2.PdfReader(file_stream)
            
            if reader.is_encrypted:
                reader.decrypt("")
            
            # Get the specific page (0-indexed)
            if page_number - 1 < len(reader.pages):
                page = reader.pages[page_number - 1]
                
                # Check if this page has annotations (form fields)
                if '/Annots' in page:
                    annotations = page['/Annots']
                    
                    for annot_ref in annotations:
                        try:
                            annot = annot_ref.get_object()
                            
                            # Check if it's a form field
                            if '/FT' in annot and '/T' in annot:
                                field_name = annot.get('/T', '')
                                field_type = annot.get('/FT', '')
                                field_value = annot.get('/V', '')
                                
                                # Clean up the field name
                                if hasattr(field_name, 'decode'):
                                    field_name = field_name.decode('utf-8')
                                elif isinstance(field_name, str) and field_name.startswith('(') and field_name.endswith(')'):
                                    field_name = field_name[1:-1]
                                
                                if field_name:
                                    # Determine input type and format
                                    if field_type == '/Tx':  # Text field
                                        if any(date_indicator in field_name.lower() for date_indicator in ['dag', 'maand', 'jaar', 'date', 'datum']):
                                            field_markup = f"[input_type=date, id={field_name}, value=none]"
                                        else:
                                            field_markup = f"[input_type=text, id={field_name}, value=none]"
                                    
                                    elif field_type == '/Btn':  # Button/Checkbox field
                                        is_checked = field_value == '/Yes' or field_value == '/On'
                                        checked_status = "true" if is_checked else "false"
                                        field_markup = f"[input_type=checkbox, id={field_name}, checked={checked_status}]"
                                    
                                    elif field_type == '/Ch':  # Choice field (dropdown/listbox)
                                        field_markup = f"[input_type=select, id={field_name}, value=none]"
                                    
                                    else:
                                        field_markup = f"[input_type=unknown, id={field_name}, value=none]"
                                    
                                    page_fields.append(field_markup)
                        
                        except Exception:
                            # Skip problematic annotations
                            continue
                
        except Exception as e:
            print(f"Error extracting form fields for page {page_number}: {e}")
        
        return page_fields

    def convert(
        self,
        file_stream: BinaryIO,
        stream_info: StreamInfo,
        **kwargs: Any,  # Options to pass to the converter
    ) -> DocumentConverterResult:
        # Check the dependencies
        if _dependency_exc_info is not None:
            raise MissingDependencyException(
                MISSING_DEPENDENCY_MESSAGE.format(
                    converter=type(self).__name__,
                    extension=".pdf",
                    feature="pdf",
                )
            ) from _dependency_exc_info[
                1
            ].with_traceback(  # type: ignore[union-attr]
                _dependency_exc_info[2]
            )

        assert isinstance(file_stream, io.IOBase)  # for mypy
        
        # Extract content page by page
        pages_content = self._extract_text_by_pages(file_stream)
        total_pages = len(pages_content)
        
        # Process each page and extract form fields for that specific page
        enhanced_content = ""
        
        for page_info in pages_content:
            page_num = page_info['page_num']
            page_text = page_info['text']
            
            # Add page demarcation
            enhanced_content += f"\n\n=====================================\nPDF PAGE {page_num}/{total_pages}\n=====================================\n\n"
            
            # Add page text
            enhanced_content += page_text
            
            # Extract and add form fields that actually exist on this specific page
            page_fields = self._extract_form_fields_for_page(file_stream, page_num)
            if page_fields:
                enhanced_content += "\n\n<!-- Form fields for this page -->\n"
                for field_markup in page_fields:
                    enhanced_content += f"{field_markup}\n"
        
        return DocumentConverterResult(
            markdown=enhanced_content,
        )
