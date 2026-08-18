# models.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class PageClassification(BaseModel):
    is_product_page: bool
    reason: str
    tech_params_count: int = 0          # количество найденных технических параметров
    has_enough_params: bool = False    

class ErrorRouting(BaseModel):
    should_return_error: bool
    error_json: str = ""

class RawSection(BaseModel):
    heading: str
    content: str

class RawTable(BaseModel):
    headers: List[str]
    rows: List[List[str]]

class RawBlocks(BaseModel):
    raw_h1: str
    raw_sections: List[RawSection] = []
    raw_tables: List[RawTable] = []
    raw_lists: List[List[str]] = []

class NameNormalization(BaseModel):
    noun: str = ""
    adjectives: List[str] = []
    brand: str = ""
    gost: str = ""
    manufacturer: str = ""
    final_product_name: str

class SectionRoutingItem(BaseModel):
    source_heading: str = ""
    semantic_route: str
    routed_text: str

class SpecItem(BaseModel):
    key: str
    value: str

class SpecTable(BaseModel):
    object_key: str
    object_value: Dict[str, str]

class SpecificationsProcessing(BaseModel):
    spec_items: List[SpecItem] = []
    spec_tables: List[SpecTable] = []

class AdditionalInfo(BaseModel):
    text: str

class ProductOutput(BaseModel):
    product_name: str = ""
    tu: str = ""
    price: str = ""
    article: str = ""
    description: str = ""
    specifications: Dict[str, Any] = {}
    colors: List[str] = []
    images: List[str] = []
    variants: List[Any] = []
    complectation: str = ""
    compatibility: str = ""
    applications: str = ""
    advantages: str = ""
    instructions_manuals: str = ""
    exploitation: str = ""
    storage: str = ""
    security_measures: str = ""
    dimensions: str = ""
    weight: str = ""
    additional_info: AdditionalInfo = Field(default_factory=lambda: AdditionalInfo(text=""))

class ReasoningOutput(BaseModel):
    step_1_page_classification: PageClassification
    step_2_error_routing: ErrorRouting
    step_3_extract_raw_blocks: RawBlocks
    step_4_name_normalization: NameNormalization
    step_5_section_routing: List[SectionRoutingItem] = []
    step_6_specifications_processing: SpecificationsProcessing
    step_7_collect_images: List[str] = []
    step_8_collect_variants: List[Dict[str, Any]] = []
    step_9_final_output: Dict[str, Any] = {}