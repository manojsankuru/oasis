from pydantic import BaseModel, Field


class ListLayersArgs(BaseModel):
    pass


class SheltersInHazardZoneArgs(BaseModel):
    pass


class PopulationWithinDistanceArgs(BaseModel):
    shelter_name: str = Field(
        description="Exact shelter name, for example 'Central Middle School'."
    )
    distance_km: float = Field(
        description="Straight-line service radius in kilometers, for example 5."
    )


class NearestShelterDistancesArgs(BaseModel):
    max_travel_km: float = Field(
        default=5.0,
        description="Acceptable travel distance in kilometers. Tracts farther than this are reported as underserved.",
    )


TOOL_ARG_MODELS = {
    "list_layers": ListLayersArgs,
    "shelters_in_hazard_zone": SheltersInHazardZoneArgs,
    "population_within_distance": PopulationWithinDistanceArgs,
    "nearest_shelter_distances": NearestShelterDistancesArgs,
}

TOOL_DESCRIPTIONS = {
    "list_layers": "List the available spatial layers with their feature counts, geometry types, CRS, and attribute names. Call this first when you do not know what data exists.",
    "shelters_in_hazard_zone": "Report which emergency shelters fall inside the flood hazard zone and how much shelter capacity is exposed versus safe.",
    "population_within_distance": "For one shelter, report the census tracts, total population, and vulnerable population within a straight-line distance in kilometers.",
    "nearest_shelter_distances": "For every census tract, report the distance to its nearest shelter and flag tracts beyond an acceptable travel distance.",
}


def _parameters(model):
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.setdefault("properties", {})
    for prop in schema["properties"].values():
        prop.pop("title", None)
    return schema


def build_tool_specs():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "parameters": _parameters(model),
            },
        }
        for name, model in TOOL_ARG_MODELS.items()
    ]


TOOL_SPECS = build_tool_specs()
