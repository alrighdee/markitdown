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

    def _extract_form_fields_data(self, file_stream: BinaryIO) -> dict:
        """Extract form field data from PDF"""
        form_fields = {}
        
        try:
            file_stream.seek(0)
            reader = PyPDF2.PdfReader(file_stream)
            
            if reader.is_encrypted:
                reader.decrypt("")
            
            # Check if the PDF has form fields
            if '/AcroForm' in reader.trailer['/Root']:
                acro_form = reader.trailer['/Root']['/AcroForm']
                
                if '/Fields' in acro_form:
                    fields = acro_form['/Fields']
                    
                    for field_ref in fields:
                        field = field_ref.get_object()
                        field_name = field.get('/T', '')
                        field_type = field.get('/FT', '')
                        field_value = field.get('/V', '')
                        
                        # Clean up the field name
                        if hasattr(field_name, 'decode'):
                            field_name = field_name.decode('utf-8')
                        elif isinstance(field_name, str) and field_name.startswith('(') and field_name.endswith(')'):
                            field_name = field_name[1:-1]
                        
                        if field_name:
                            # Determine input type and format
                            if field_type == '/Tx':  # Text field
                                # Check if it looks like a date field
                                if any(date_indicator in field_name.lower() for date_indicator in ['dag', 'maand', 'jaar', 'date', 'datum']):
                                    form_fields[field_name] = f"[input_type=date, id={field_name}, value=none]"
                                else:
                                    form_fields[field_name] = f"[input_type=text, id={field_name}, value=none]"
                            
                            elif field_type == '/Btn':  # Button/Checkbox field
                                is_checked = field_value == '/Yes' or field_value == '/On'
                                checked_status = "true" if is_checked else "false"
                                form_fields[field_name] = f"[input_type=checkbox, id={field_name}, checked={checked_status}]"
                            
                            elif field_type == '/Ch':  # Choice field (dropdown/listbox)
                                form_fields[field_name] = f"[input_type=select, id={field_name}, value=none]"
                            
                            else:
                                form_fields[field_name] = f"[input_type=unknown, id={field_name}, value=none]"
                
        except Exception as e:
            print(f"Error extracting form fields: {e}")
        
        return form_fields

    def _distribute_fields_to_pages(self, form_fields: dict, total_pages: int) -> dict:
        """Distribute form fields across pages more evenly"""
        # Group fields by some logical distribution
        # This is a simple approach - in reality we'd want to use the PDF's internal positioning
        
        fields_per_page = {}
        field_list = list(form_fields.items())
        
        if not field_list:
            return fields_per_page
        
        # Simple distribution: spread fields across all pages
        fields_per_page_count = len(field_list) // total_pages
        remainder = len(field_list) % total_pages
        
        current_index = 0
        for page_num in range(1, total_pages + 1):
            fields_for_this_page = fields_per_page_count
            if remainder > 0:
                fields_for_this_page += 1
                remainder -= 1
            
            page_fields = {}
            for _ in range(fields_for_this_page):
                if current_index < len(field_list):
                    field_name, field_markup = field_list[current_index]
                    page_fields[field_name] = field_markup
                    current_index += 1
            
            if page_fields:
                fields_per_page[page_num] = page_fields
        
        return fields_per_page

    def _insert_form_fields_in_page(self, page_text: str, page_fields: dict, page_num: int) -> str:
        """Insert form fields inline in page text where they likely belong"""
        
        if not page_fields:
            return page_text
            
        lines = page_text.split('\n')
        enhanced_lines = []
        used_fields = set()
        
        for line in lines:
            enhanced_lines.append(line)
            line_lower = line.lower().strip()
            
            # Skip empty lines
            if not line_lower:
                continue
            
            # Look for form fields that might belong on this line - be more conservative
            for field_name, field_markup in page_fields.items():
                if field_name in used_fields:
                    continue
                    
                field_name_lower = field_name.lower()
                
                # Be more specific with matching
                matched = False
                
                # Direct keyword matches with better context
                if 'plaats' in field_name_lower and 'plaats' in line_lower:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                elif 'naam' in field_name_lower and any(word in line_lower for word in ['naam', 'name']) and len(line_lower) < 100:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                elif 'datum' in field_name_lower and any(word in line_lower for word in ['datum', 'date']) and len(line_lower) < 100:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                elif 'tbc' in field_name_lower and 'tbc' in line_lower:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                elif 'geslacht' in field_name_lower and any(word in line_lower for word in ['geslacht', 'man', 'vrouw']) and len(line_lower) < 100:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                elif 'geboorte' in field_name_lower and 'geboorte' in line_lower:
                    enhanced_lines.append(f"\n{field_markup}\n")
                    used_fields.add(field_name)
                    matched = True
                
                if matched:
                    break
        
        # Add any remaining fields for this page at the end of the page
        remaining_page_fields = [field_markup for field_name, field_markup in page_fields.items() if field_name not in used_fields]
        if remaining_page_fields:
            enhanced_lines.append("\n<!-- Additional fields for this page -->")
            for field_markup in remaining_page_fields:
                enhanced_lines.append(field_markup)
        
        return '\n'.join(enhanced_lines)

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
        
        # Extract form fields data
        form_fields_data = self._extract_form_fields_data(file_stream)
        
        # Distribute form fields across pages
        fields_per_page = self._distribute_fields_to_pages(form_fields_data, total_pages)
        
        # Process each page
        enhanced_content = ""
        
        for page_info in pages_content:
            page_num = page_info['page_num']
            page_text = page_info['text']
            
            # Add page demarcation
            enhanced_content += f"\n\n=====================================\nPDF PAGE {page_num}/{total_pages}\n=====================================\n\n"
            
            # Get fields for this page
            page_fields = fields_per_page.get(page_num, {})
            
            # Insert form fields inline for this page
            enhanced_page_text = self._insert_form_fields_in_page(page_text, page_fields, page_num)
            
            enhanced_content += enhanced_page_text
        
        return DocumentConverterResult(
            markdown=enhanced_content,
        )
