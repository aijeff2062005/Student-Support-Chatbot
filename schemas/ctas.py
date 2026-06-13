from pydantic import BaseModel, Field


class Condition(BaseModel):
    name: str = Field(...)
    description: str | None = Field(default=None)


class Button(BaseModel):
    name: str
    link: str | None = Field(default=None)


class ButtonResponse(BaseModel):
    buttons: list[Button]
